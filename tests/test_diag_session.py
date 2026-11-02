# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag's ModuleSession and scan over the scripted TP 2.0 mock ECU."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mock_tp20 import MockTp20Ecu  # noqa: E402

from ovtsuite.flash import tp20_transport as TT
from ovtsuite.diag import defs as D
from ovtsuite.diag import link as L
from ovtsuite.diag import scan as SC
from ovtsuite.diag.model import Vehicle
from ovtsuite.diag.session import ModuleSession, parse_9b

RECORD_9B = (b"\x9b" + b"03G906016LQ " + b"8449" + b"\x03" + (71).to_bytes(3, "big")
             + ((987654 << 27) | (345 << 17) | 45678).to_bytes(6, "big") + b"R4 1,9L EDC G000SG")
GROUP1 = bytes.fromhex("6101") + bytes.fromhex("010102 040506 190a0b 250000")


def _engine_handler(seen, faults):
    def handler(req):
        seen.append(bytes(req))
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req == b"\x1a\x9b":
            return b"\x5a" + RECORD_9B
        if req == b"\x21\x01":
            return GROUP1
        if req == b"\x18\x02\xff\x00":
            body = b"".join((c >> 8).to_bytes(1, "big") + (c & 0xFF).to_bytes(1, "big") + bytes([st])
                            for c, st in faults)
            return b"\x58" + bytes([len(faults)]) + body
        if req == b"\x14\xff\x00":
            faults.clear()
            return b"\x54\xff\x00"
        if req == b"\x3e":
            return b"\x7e"
        return b"\x7f" + bytes([req[0], 0x11])
    return handler


def _resolver():
    return D.Resolver(D.load_packs([D.bundled_dir()], "bundled"))


def _row(res, addr="01", gen="C6"):
    return res.module_address(addr, gen)


def test_session_identifies_reads_and_clears_faults_over_can():
    # three VAG numbers: two that stand for P-codes and 01044, which stands for none
    seen, faults = [], [(16785, 0x60), (16683, 0x60), (0x0414, 0x24)]
    ecu = MockTp20Ecu(_engine_handler(seen, faults))
    res = _resolver()
    row = _row(res)
    link = L.Link(TT.CanKLine(ecu, dest=0x01), [], "can")
    sess = ModuleSession(link, row, res, generation="C6")
    sess.connect()
    assert seen[0] == b"\x10\x89"                       # the engine's session byte from the module doc
    info = sess.info
    assert info.reached and info.protocol == "kwp2000"
    assert info.part == "03G906016LQ" and info.sw == "8449"
    assert info.coding == 71 and info.wsc == 45678
    assert info.extra["importer"] == 345 and info.extra["equipment"] == 987654
    assert sess.moddef["variants"]["adaptation"] == "routine_b8"
    got = sess.read_faults()
    # no document says otherwise, so the words are VAG numbers (16683 = P0299, 01044 = no P-code),
    # whatever their magnitude: 0x0414 is not P0414 here
    assert [f.vag for f in got] == [16785, 16683, 1044]
    assert [f.sae for f in got] == ["P0401", "P0299", ""]
    assert got[1].text                                     # standard P-code text
    assert got[0].status == 0x60 and got[0].proto == "kwp2000"
    assert got[2].display_code == "01044"
    named = sess.read_group_named(1)
    assert len(named) == 3 and named[0][1].fid == 0x01
    sess.clear_faults()
    assert sess.read_faults() == [] and info.faults == []
    sess.close()
    assert not ecu.opened


SAE_ENGINE_DOC = """kind: module
match: { addr: "01", protocol: kwp2000 }
faults: { encoding: sae }
"""


def _user_pack(tmp_path, doc):
    root = tmp_path / "mine"
    root.mkdir()
    (root / "pack.yaml").write_text("format: 1\nid: mine\nversion: 1.0.0\n", encoding="utf-8")
    (root / "m.yaml").write_text(doc, encoding="utf-8")
    return D.load_pack(str(root), "user")


def test_kwp2000_fault_words_follow_the_module_document(tmp_path):
    """The same two words, 0x0214 and 0x0414, are VAG 00532 / 01044 on a module whose
    document says ``vag`` (or says nothing) and P0214 / P0414 on one whose document
    says ``sae``: the definition decides, never the magnitude."""
    words = [(0x0214, 0x60), (0x0414, 0x60), (0x0299, 0x24)]

    def read(res):
        ecu = MockTp20Ecu(_engine_handler([], list(words)))
        sess = ModuleSession(L.Link(TT.CanKLine(ecu, dest=0x01), [], "can"), _row(res), res, generation="C6")
        sess.connect()
        try:
            return sess.read_faults()
        finally:
            sess.close()

    plain = read(_resolver())
    assert [(f.vag, f.sae) for f in plain] == [(532, ""), (1044, ""), (665, "")]
    assert plain[0].display_code == "00532"
    packs = D.load_packs([D.bundled_dir()], "bundled") + [_user_pack(tmp_path, SAE_ENGINE_DOC)]
    sae = read(D.Resolver(packs))
    assert [(f.vag, f.sae) for f in sae] == [(16598, "P0214"), (16798, "P0414"), (16683, "P0299")]
    assert sae[2].text                                     # the standard P0299 text found through the SAE word


def test_c6_body_module_words_are_vag_numbers():
    """The bundled C6 cluster document says ``vag``: 0x0414 from the cluster is 01044."""
    def handler(req):
        if req == b"\x10\x89":
            return b"\x50\x89"
        if req == b"\x18\x02\xff\x00":
            return b"\x58\x01\x04\x14\x60"
        return b"\x7f" + bytes([req[0], 0x11])
    res = _resolver()
    ecu = MockTp20Ecu(handler, dest=0x07)
    sess = ModuleSession(L.Link(TT.CanKLine(ecu, dest=0x07), [], "can"), _row(res, "17"), res, generation="C6")
    sess.connect()
    assert sess.moddef["faults"]["encoding"] == "vag"
    got = sess.read_faults()
    assert [(f.vag, f.sae) for f in got] == [(1044, "")]
    sess.close()


def test_kw1281_words_are_always_vag_numbers():
    """A KW1281 module's fault words are VAG numbers whatever any document says."""
    from mock_kw1281 import Link as KLink, MockKw1281Module
    from ovtsuite.diag.session import decode_fault_word, fault_word_encoding
    ecu = MockKw1281Module(address=0x01, baud=9600, part="038906012GN", desc="1.9l R4 EDC 00SG 2060",
                           coding=1, wsc=0)
    ecu.dtcs = [(0x0214, 0x23), (16485, 0x23), (0x0414, 0xA3)]          # 00532, 16485 = P0101, 01044 sporadic
    res = _resolver()
    sess = ModuleSession(L.Link(KLink(ecu), [], "kline"), res.module_address("01", None), res)
    sess.connect()
    assert sess.protocol == "kw1281"
    got = sess.read_faults()
    assert [(f.vag, f.sae, f.proto) for f in got] == [(532, "", "kw1281"), (16485, "P0101", "kw1281"),
                                                      (1044, "", "kw1281")]
    assert got[2].intermittent
    sess.close()
    # the helpers the session and the assistant's tool share
    assert fault_word_encoding({"faults": {"encoding": "sae"}}, "EDC16", "kw1281") == "vag"
    assert fault_word_encoding({"faults": {"encoding": "sae"}}, "EDC16", "kwp2000") == "sae"
    assert fault_word_encoding({}, "EDC15", "kwp2000") == "vag"
    assert fault_word_encoding({}, "EDC16", "kwp2000") == "vag"
    assert fault_word_encoding({"faults": {"encoding": "odd"}}, "", "kwp2000") == "vag"
    assert decode_fault_word(0x0414, "kwp2000", "vag") == (1044, "")
    assert decode_fault_word(0x0414, "kwp2000", "sae") == (16798, "P0414")
    assert decode_fault_word(0x4014, "kwp2000", "sae") == (None, "C0014")       # no VAG number for a C-code
    assert decode_fault_word(0x4014, "kwp2000", "vag") == (16404, "P0020")
    assert decode_fault_word(0x0299, "kw1281", "sae") == (665, "")


def test_session_tcu_session_byte():
    seen = []
    def handler(req):
        seen.append(bytes(req))
        if req == b"\x10\x86":
            return b"\x50\x86"
        if req == b"\x1a\x9b":
            return b"\x5a\x9b" + b"09L927156AB " + b"0036"
        return b"\x7f" + bytes([req[0], 0x11])
    ecu = MockTp20Ecu(handler, dest=0x02)
    res = _resolver()
    link = L.Link(TT.CanKLine(ecu, dest=0x02), [], "can")
    sess = ModuleSession(link, _row(res, "02"), res, generation="C6")
    sess.connect()
    assert seen[0] == b"\x10\x86"
    assert sess.info.part == "09L927156AB" and sess.info.sw == "0036"
    assert sess.info.coding is None                     # short record: no coding/WSC fields
    sess.close()


def test_parse_9b_short_and_garbage():
    assert parse_9b(b"") == {}
    assert parse_9b(b"\x9b" + b"038906012GN " + b"2060")["part"] == "038906012GN"
    assert parse_9b(b"\x5a\x9b")== {}


def test_scan_vehicle_over_can(monkeypatch):
    res = _resolver()
    ecus = {}

    def fake_open_link(iface, transport, module, log=None, **kw):
        addr = module["addr"]
        if addr == "01":
            ecu = MockTp20Ecu(_engine_handler([], [(16785, 0x60)]), dest=0x01)
        elif addr == "02":
            ecu = MockTp20Ecu(lambda req: b"\x50\x86" if req == b"\x10\x86" else
                              (b"\x5a\x9b" + b"09L927156AB " + b"0036" if req == b"\x1a\x9b" else
                               b"\x7f" + bytes([req[0], 0x11])), dest=0x02)
        else:
            raise OSError("no answer from 0x%s" % addr)
        ecus[addr] = ecu
        return L.Link(TT.CanKLine(ecu, dest=int(addr, 16)), [], "can")

    monkeypatch.setattr(SC, "open_link", fake_open_link)
    iface = L.Interface("fake", "Fake CAN device", "j2534")
    vehicle = Vehicle(id="a6", name="Audi A6 (4F)", generation="C6")
    progress = []
    result = SC.scan_vehicle(iface, "can", vehicle, res, progress=lambda m, d, t: progress.append((m, d, t)))
    addrs = [m.addr for m in result.modules]
    # only rows with a TP 2.0 address take part on CAN: for the C6 that is the whole gateway table
    # (no shared bus could be opened on the fake device, so this is the sequential path without a gateway list)
    assert addrs == sorted(addrs) and {"01", "02", "03", "08", "17", "46"} <= set(addrs) and "25" not in addrs
    assert result.basis == "table" and result.gateway is None
    by = {m.addr: m for m in result.modules}
    assert by["01"].reached and by["01"].fault_count == 1 and by["01"].part == "03G906016LQ"
    assert by["02"].reached and by["02"].fault_count == 0
    assert by["03"].reached is False and "no answer" in by["03"].error
    assert result.fault_total == 1 and len(result.reached) == 2
    assert progress[-1][0] == "done"
    assert all(not e.opened for e in ecus.values())
    # round-trips through the garage's JSON form
    from ovtsuite.diag.model import ScanResult
    again = ScanResult.from_dict(result.to_dict())
    assert again.module("01").faults[0].vag == 16785 and again.module("03").reached is False

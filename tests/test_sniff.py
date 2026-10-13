# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""TP 2.0 / KWP decoding, capture files, J2534 structures."""
import ctypes
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.sniff import tp20 as T  # noqa: E402
from ovtsuite.sniff import capture as C  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402


def _frames():
    """A VCDS-style session: setup, params, 1A 9B request, multi-frame reply."""
    t = 0.0
    fr = []

    def add(cid, hexs, tx=False):
        nonlocal t
        t += 0.002
        fr.append(T.Frame(t, cid, bytes.fromhex(hexs.replace(" ", "")), tx))
    add(0x200, "01 C0 00 10 00 03 01")           # tester: listen on 0x300
    add(0x201, "00 D0 00 03 40 07 01")           # ECU: sends on 0x300, listens on 0x740
    add(0x740, "A0 0F 8A FF 32 FF")              # params request
    add(0x300, "A1 0F 8A FF 4A FF")              # params response
    add(0x740, "10 00 02 1A 9B")                 # last frame, ack expected: len 2, 1A 9B
    add(0x300, "B1")                             # ack, next seq 1
    add(0x300, "20 00 13 5A 9B 30 33 38")        # more, no ack: len 0x13 = 19
    add(0x300, "21 39 30 36 30 31 36 41")
    add(0x300, "12 42 20 20 34 39 39 39")        # last, ack expected (19 bytes total)
    add(0x740, "B3")
    add(0x740, "A3")                             # keep-alive
    add(0x300, "A1 0F 8A FF 4A FF")
    add(0x740, "A8")                             # disconnect
    add(0x7E8, "02 41 0C")                       # unrelated frame
    return fr


def test_tp20_decode_session():
    dec = T.Tp20Decoder()
    evs = []
    for f in _frames():
        evs += dec.feed(f)
    kinds = [e.kind for e in evs]
    assert kinds[0] == "setup" and "0x300" in evs[0].text
    assert kinds[1] == "setup-ack" and "0x740" in evs[1].text and "0x300" in evs[1].text
    assert kinds[2] == "param" and evs[2].channel == "T->E" and "BS 15" in evs[2].text
    assert kinds[3] == "param" and evs[3].channel == "E->T"
    msgs = [e for e in evs if e.kind == "message"]
    assert len(msgs) == 2
    assert msgs[0].payload == bytes.fromhex("1A9B") and msgs[0].channel == "T->E"
    assert msgs[0].text.startswith("readEcuIdentification 9b")
    assert msgs[1].channel == "E->T" and len(msgs[1].payload) == 19
    assert msgs[1].payload[:2] == bytes.fromhex("5A9B")
    assert msgs[1].payload[2:].decode() == "038906016AB  4999"
    assert msgs[1].text.startswith("readEcuIdentification + 9b")
    acks = [e for e in evs if e.kind == "ack"]
    assert len(acks) == 2 and "next seq 1" in acks[0].text
    assert any(e.kind == "param" and "disconnect" in e.text for e in evs)
    assert evs[-1].kind == "raw"                   # 0x7E8 is not a TP channel
    assert not dec.channels                        # closed by A8


def test_tp20_timing_and_refusal():
    assert T._tval(0x8A) == "100 ms"          # 10 ms units x 10 (VCDS T1)
    assert T._tval(0x32) == "5 ms"
    dec = T.Tp20Decoder()
    dec.feed(T.Frame(0.0, 0x200, bytes.fromhex("01C00010000301")))
    ev = dec.feed(T.Frame(0.1, 0x201, bytes.fromhex("00D60003400701")))[0]
    assert ev.kind == "setup-ack" and "refused" in ev.text and not dec.channels


def test_kwp_descriptions():
    assert T.describe_kwp(bytes.fromhex("1089")) == "startDiagnosticSession 89"
    assert T.describe_kwp(bytes.fromhex("5089")) == "startDiagnosticSession + 89"
    assert T.describe_kwp(bytes.fromhex("7F2178")) == "NAK readDataByLocalId: responsePending"
    assert T.describe_kwp(bytes.fromhex("3E")) == "testerPresent"
    d, txt = T.decode_kline(bytes.fromhex("8110F13E"))
    assert d == "T->E" and txt == "testerPresent"
    d, txt = T.decode_kline(bytes.fromhex("83F1105089"))
    assert d == "E->T" and txt.startswith("startDiagnosticSession +")
    d, txt = T.decode_kline(bytes.fromhex("80F11003610100"))       # length byte form
    assert d == "E->T" and txt.startswith("readDataByLocalId + 01")


def test_capture_roundtrip_and_candump():
    recs = [C.Record(1757160000.123456, "can", 0x200, bytes.fromhex("01C00010000301")),
            C.Record(1757160000.124, "can", 0x18DAF110, b"\x02\x01\x0c", tx=True, extended=True),
            C.Record(1757160001.5, "kline", 0, bytes.fromhex("8110F13E"))]
    d = tempfile.mkdtemp()
    p = os.path.join(d, "cap.log")
    C.write(p, recs, "test capture")
    lines = open(p).read().splitlines()
    assert lines[0] == "# test capture"
    assert lines[1] == "(1757160000.123456) can 200#01C00010000301"
    assert lines[2] == "(1757160000.124000) can-tx 18DAF110#02010C"
    assert lines[3] == "(1757160001.500000) kline 0#8110F13E"
    back = C.read(p)
    assert [(r.bus, r.can_id, r.data, r.tx, r.extended) for r in back] == \
        [(r.bus, r.can_id, r.data, r.tx, r.extended) for r in recs]
    r = C.parse_line("(1699999999.000100) can0 7E8#0641 0C1AF8")
    assert r is None                                  # candump data has no spaces
    r = C.parse_line("(1699999999.000100) can0 7E8#06410C1AF8")
    assert r.bus == "can" and r.can_id == 0x7E8 and r.data == bytes.fromhex("06410C1AF8")


def test_j2534_structs_and_msg():
    assert ctypes.sizeof(J.PASSTHRU_MSG) == 6 * 4 + 4128   # ULONG is 32-bit in J2534
    m = J.PassThru._msg(J.CAN, (0x200).to_bytes(4, "big") + b"\x01\xc0", 0)
    assert m.ProtocolID == J.CAN and m.DataSize == 6
    msg = J.Msg(J.CAN, 0, 0, 1000, bytes(m.Data[:m.DataSize]))
    assert msg.can_id == 0x200 and msg.payload == b"\x01\xc0" and not msg.is_tx_echo
    assert J.Msg(J.ISO14230, J.TX_MSG_TYPE, 0, 0, b"\x81\x10\xf1\x3e").is_tx_echo
    assert J.list_devices() == [] or os.name == "nt"


# ------------------------------------------------------------- K-line tap
from ovtsuite.sniff import kline as KL  # noqa: E402


def _cks(h):
    b = bytes.fromhex(h.replace(" ", ""))
    return b + bytes([sum(b) & 0xFF])


def test_kline_framer_kwp_and_speed_follow():
    """Bytes as a passive FTDI tap sees them, one burst per read."""
    bursts = [(0.00, bytes.fromhex("55")), (0.02, bytes.fromhex("EF")), (0.03, bytes.fromhex("8F")),
              (0.05, bytes.fromhex("70")), (0.08, bytes.fromhex("FE")),
              (0.16, bytes.fromhex("8210F11089")), (0.18, bytes.fromhex("1C")),      # 10 89 + cks
              (0.20, bytes.fromhex("82F1")), (0.21, bytes.fromhex("1050895C")),
              (0.30, _cks("83 10 F1 10 86 87")), (0.31, _cks("83 F1 10 50 86 87")),
              (0.40, bytes.fromhex("80F11003610100") + bytes([(0x80 + 0xF1 + 0x10 + 3 + 0x61 + 1) & 0xFF]))]
    f = KL.Framer()
    out = []
    for t, b in bursts:
        out += f.feed(t, b)
    kinds = [x.kind for x in out]
    assert kinds == ["init", "kwp", "kwp", "kwp", "kwp", "kwp"]
    assert "2031" in out[0].note and out[0].data == bytes.fromhex("55EF8F70FE")
    assert out[1].data == bytes.fromhex("8210F11089")            # checksum stripped
    assert f.new_baud == 124800 and "124800" in out[4].note
    assert out[5].data == bytes.fromhex("80F11003610100")        # explicit length byte form
    assert KL.speed_byte_to_baud(0x50) == 38400 and KL.speed_byte_to_baud(0xA7) == 249600


def test_kline_framer_resync_and_kw1281():
    f = KL.Framer()
    junk = bytes.fromhex("FC")
    out = f.feed(0.0, junk)
    assert out == []                                  # could still be a frame start
    out = f.feed(0.5, _cks("81 10 F1 3E"))
    assert [x.kind for x in out] == ["raw", "kwp"]   # the junk flushed after the gap
    assert out[1].data == bytes.fromhex("8110F13E")
    blk = b""
    for x in (0x05, 0x01, 0xF6, 0x41, 0x42):
        blk += bytes([x, (~x) & 0xFF])
    blk += b"\x03"
    f2 = KL.Framer()
    out = f2.feed(0.0, bytes.fromhex("55018A75") + blk)
    assert [x.kind for x in out] == ["init", "kw1281"]
    assert out[1].data == bytes.fromhex("0501F64142") and "KW1281" in out[0].note


def test_capture_reads_kwhack_kline_sniffer_log():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "mpps.log")
    with open(p, "w") as fh:
        fh.write("=== kline_sniffer.py capture started: port=COM4 baud=10400 mode=fixed-baud ===\n"
                 "[t=31428.695716]    5 bytes: 82 10 F1 10 89\n"
                 "[t=31428.712195]    1 bytes: 1C\n"
                 "=== BAUD CHANGED (negotiated): 10400 -> 38400 (startDiagnosticSession session=0x85 speed=0x50) ===\n")
    recs = C.read(p)
    assert [(r.bus, r.data.hex()) for r in recs] == [("kraw", "8210f11089"), ("kraw", "1c")]
    f = KL.Framer()
    out = []
    for r in recs:
        out += f.feed(r.t, r.data)
    assert len(out) == 1 and out[0].kind == "kwp"
    # a kraw record survives the native capture format
    p2 = os.path.join(d, "native.log")
    C.write(p2, recs)
    assert [r.bus for r in C.read(p2)] == ["kraw", "kraw"]

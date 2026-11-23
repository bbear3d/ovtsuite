# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""CAN channel on the SM2 (ovtsuite/flash/sm2can.py) against a fake device that
answers like the one traced on the bench (docs/SM2_USB_PROTOCOL.md)."""
import struct

import pytest

from ovtsuite.flash import sm2can as C
from ovtsuite.flash import sm2usb as S

STATUS = bytes.fromhex("80020000 00000000 80020100 ff010000 ff000000")


def record(cid, data):
    return struct.pack("<II", len(data), cid) + data.ljust(8, b"\0")


class FakeCanSm2:
    def __init__(self):
        self.sent = []
        self.buf = bytearray()
        self.out = bytearray()
        self.rx_frames = []                 # records the next poll will carry

    def write(self, data):
        self.buf += data
        while len(self.buf) >= 4:
            cmd, n = self.buf[0], self.buf[1] | (self.buf[2] << 8)
            assert self.buf[3] == S.checksum(cmd, n)
            if len(self.buf) < 4 + n:
                return
            body = bytes(self.buf[4:4 + n])
            del self.buf[:4 + n]
            self.sent.append((cmd, body))
            self.out += S.pack(0, self.answer(cmd, body))

    def answer(self, cmd, body):
        if cmd == S.CMD_PING:
            return body
        if cmd == S.CMD_INFO:
            p = bytearray(18)
            struct.pack_into("<I", p, 0, 0x0A111788)
            p[16] = 2
            return bytes(p)
        if cmd == C.CMD_CFG3:
            return STATUS
        if cmd == C.CMD_APPLY:
            return b"\x80\x02\x00\x00"
        if cmd == C.CMD_CAN:
            recs, self.rx_frames = b"".join(self.rx_frames), []
            return STATUS + recs
        return b""

    def read(self, timeout_ms):
        if not self.out:
            return b""
        d, self.out = bytes(self.out[:64]), self.out[64:]
        return d

    def close(self):
        pass


def open_channel(baud=500000):
    t = FakeCanSm2()
    return t, C.Sm2CanChannel(S.Sm2(t), baud)


def test_block1_carries_the_baud_rate_and_the_dll_constants():
    b = C.config_block1(500000)
    assert len(b) == 0x130
    assert struct.unpack_from("<I", b, 124)[0] == 500000
    assert (b[120], b[121], b[122], b[134], b[136]) == (1, 2, 1, 0x0A, 0x0A)
    assert struct.unpack_from("<I", C.config_block1(250000), 124)[0] == 250000
    f = C.config_block1(500000, 1)
    assert len(f) == 0x130 + 0x3B and f[132] == 1
    assert f[304:308] == bytes.fromhex("00000080")


def test_open_sequence_matches_the_traced_dll():
    t, ch = open_channel()
    seq = [(c, len(b)) for c, b in t.sent if c not in (S.CMD_PING, S.CMD_INFO)]
    assert seq == [(0x88, 0), (0x50, 0x130), (0x51, 0xB8), (0x52, 0xB8),      # open
                   (0x50, 0x130), (0x51, 0xB8), (0x52, 0xB8), (0x53, 2),     # connect
                   (0x50, 0x16B), (0x51, 0xB8), (0x52, 0xB8)]                # pass filter
    apply_ = [b for c, b in t.sent if c == 0x53][0]
    assert apply_ == bytes.fromhex("3000")
    assert ch.handle == b"\x80\x02"
    ch.disconnect()
    assert t.sent[-1] == (0x53, bytes.fromhex("1000"))


def test_transmit_frame_layout():
    t, ch = open_channel()
    ch.write_can(0x7E0, bytes.fromhex("02 10 03 00 00 00 00 00"))
    cmd, body = t.sent[-1]
    assert cmd == 0x1C
    assert body.hex() == "c0 8002 08000000 e0070000 0210030000000000".replace(" ", "")
    ch.write_can(0x200, bytes.fromhex("01 c0 00 10 00 03 01"))
    assert t.sent[-1][1].hex() == "c0800207000000" "00020000" "01c0001000030" "1"


def test_received_records_become_messages_and_poll_op_follows_the_data():
    t, ch = open_channel()
    t.rx_frames = [record(0x201, bytes.fromhex("00d00003400701")), record(0x288, bytes(range(8)))]
    msgs = ch.read(10, 50)
    assert [(m.can_id, m.payload) for m in msgs] == [
        (0x201, bytes.fromhex("00d00003400701")), (0x288, bytes(range(8)))]
    ch.read(10, 1)                                    # previous reply had frames: ack with 0xC0
    ch.read(10, 1)                                    # that one was empty: plain 0x80
    polls = [b[0] for c, b in t.sent if c == 0x1C]
    assert polls[:3] == [0x80, 0xC0, 0x80]


def test_software_id_filter_drops_other_ids():
    t, ch = open_channel()
    ch.pass_ids([0x201, 0x300])
    t.rx_frames = [record(0x280, bytes(8)), record(0x300, b"\xa1\x0f")]
    assert [m.can_id for m in ch.read(10, 50)] == [0x300]
    ch.pass_all()
    t.rx_frames = [record(0x280, bytes(8))]
    assert [m.can_id for m in ch.read(10, 50)] == [0x280]


def test_parse_status_ignores_a_partial_record():
    p = STATUS + record(0x300, b"\x01") + b"\x00" * 5
    assert C.parse_status(p) == [(0x300, b"\x01")]


def test_extended_ids_are_refused():
    with pytest.raises(S.Sm2Error):
        C.Sm2CanChannel(S.Sm2(FakeCanSm2()), extended=True)
    t, ch = open_channel()
    with pytest.raises(S.Sm2Error):
        ch.write_can(0x18DA00F1, b"\0", extended=True)


def test_background_poller_keeps_polling_while_the_caller_is_idle():
    """TP 2.0 pauses 0.25 s between channels; the device must still be polled then."""
    import time
    t = FakeCanSm2()
    ch = C.Sm2CanChannel(S.Sm2(t), 500000, background=True)
    try:
        t.rx_frames.append(record(0x201, bytes.fromhex("00D0000340070101")))
        end = time.monotonic() + 2
        while not ch._rx and time.monotonic() < end:
            time.sleep(0.01)                     # the caller does nothing
        assert [m.can_id for m in ch.read(8, 0)] == [0x201]
    finally:
        ch.disconnect()
    assert not ch._thread.is_alive()

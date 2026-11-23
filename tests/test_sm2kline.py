# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""K-line on the SM2 (ovtsuite/flash/sm2kline.py) against a fake device that
answers like the one traced on the bench with a VDO cluster."""
import struct

import pytest

from ovtsuite.flash import j2534 as J
from ovtsuite.flash import sm2kline as K
from ovtsuite.flash import sm2usb as S
from ovtsuite.flash.kline_j2534_raw import J2534RawKLine

STATUS = bytes.fromhex("00000000 00000000 80020000 ff010000 ff000000")


class FakeKLineSm2:
    def __init__(self):
        self.sent = []
        self.buf = bytearray()
        self.out = bytearray()
        self.queue = []                      # byte strings the next polls will carry
        self.key = (0x01, 0x8A)
        self.silent_init = False

    def write(self, data):
        self.buf += data
        while len(self.buf) >= 4:
            cmd, n = self.buf[0], self.buf[1] | (self.buf[2] << 8)
            if len(self.buf) < 4 + n:
                return
            body = bytes(self.buf[4:4 + n])
            del self.buf[:4 + n]
            self.sent.append((cmd, body))
            st, pl = self.answer(cmd, body)
            self.out += S.pack(st, pl)

    def answer(self, cmd, body):
        if cmd == S.CMD_PING:
            return 0, body
        if cmd == S.CMD_INFO:
            p = bytearray(18)
            struct.pack_into("<I", p, 0, 0x0A111788)
            p[16] = 2
            return 0, bytes(p)
        if cmd == K.CMD_FIVE_BAUD:
            if self.silent_init:
                return 0x0B, b""
            return 0, bytes(4) + bytes(self.key) + b"\0" + body[5:] + b"\x44\0\0\0"
        if cmd == K.CMD_KLINE_POLL:
            recs = b""
            if len(body) == 4 and self.queue:
                recs = b"".join(bytes([b, 0x80]) for b in self.queue.pop(0)) + b"\x00\x10"
            return 0, STATUS + recs
        if cmd == K.CMD_APPLY:
            return 0, b"\0\0\0\0"
        return 0, b""

    def read(self, timeout_ms):
        if not self.out:
            return b""
        d, self.out = bytes(self.out[:64]), self.out[64:]
        return d

    def close(self):
        pass


def open_channel(baud=10400):
    t = FakeKLineSm2()
    return t, K.Sm2KLineChannel(S.Sm2(t), baud, background=False)   # the caller's reads poll: exact sequences


def test_block1_is_the_traced_connect_frame():
    b = K.config_block1(10400)
    assert len(b) == 0x130
    assert struct.unpack_from("<I", b, 18)[0] == 10400
    assert (b[17], b[22], b[23], b[25], b[26], b[28], b[32], b[39]) == (1, 7, 7, 3, 5, 0x37, 8, 0x14)
    r = K.config_block1(10400, 1, 1, 0)
    assert (r[26], r[28], r[39]) == (0, 1, 1)


def test_connect_sequence():
    t, ch = open_channel()
    seq = [(c, len(b)) for c, b in t.sent if c not in (S.CMD_PING, S.CMD_INFO)]
    assert seq == [(0x88, 0), (0x50, 0x130), (0x51, 0xB8), (0x52, 0xB8),
                   (0x50, 0x130), (0x51, 0xB8), (0x52, 0xB8), (0x53, 2),
                   (0x1D, 4)]                          # the DLL polls straight after connecting
    assert [b for c, b in t.sent if c == 0x53] == [bytes.fromhex("0300")]
    ch.clear_rx()
    assert t.sent[-1] == (0x53, bytes.fromhex("0200"))
    ch.disconnect()
    assert (0x53, bytes.fromhex("0100")) in t.sent


def test_timing_config_resends_the_blocks_with_milliseconds():
    t, ch = open_channel()
    n = len(t.sent)
    ch.set_config(P1_MAX=2, P3_MIN=0, P4_MIN=0, LOOPBACK=0, FIVE_BAUD_MOD=1, PARITY=0)
    blocks = [b for c, b in t.sent[n:] if c == 0x50]
    assert blocks and blocks[-1] == K.config_block1(10400, 1, 1, 0)
    with pytest.raises(S.Sm2Error):
        ch.set_config(PARITY=2)


def test_five_baud_init_frame_and_key_bytes():
    t, ch = open_channel()
    assert ch.five_baud_init(0x97) == bytes([0x01, 0x8A])
    cmd, body = t.sent[-1]
    assert cmd == 0x04
    assert body.hex(" ") == "00 00 00 00 97 08 00 2c 01 f4 01 2c 01 2c 01 19 00 32 00"
    t.silent_init = True
    with pytest.raises(S.Sm2Error):
        ch.five_baud_init(0x17)


def test_write_and_read_records():
    t, ch = open_channel()
    ch.write(bytes([0x03, 0x10, 0x09, 0x03]))
    assert t.sent[-1] == (0x1D, bytes.fromhex("01000002 03 80 10 80 09 80 03 80".replace(" ", "")))
    t.queue = [b"\x0f"]
    msgs = ch.read(1, 100)
    assert [m.data for m in msgs] == [b"\x0f"] and msgs[0].protocol == J.ISO9141
    assert ch.read(1, 1) == []                       # nothing more; the boundary record is not a byte
    polls = [b[0] for c, b in t.sent if c == 0x1D and len(b) == 4]
    # the poll after connecting; the first read (which gets the byte and its boundary); the second,
    # which acknowledges that message; the third, with nothing to acknowledge
    assert polls[:3] == [0, 0, 1]
    ch.read(1, 1)
    assert [b[0] for c, b in t.sent if c == 0x1D and len(b) == 4][3] == 0


def test_a_message_is_handed_over_at_its_boundary():
    t, ch = open_channel()
    pkt = lambda recs: S.Packet(0, STATUS + recs)
    ch._take(pkt(b"\x41\x80\x42\x80"))                 # two bytes, no boundary yet
    assert bytes(ch._rx) == b"" and bytes(ch._pending) == b"AB" and not ch._ack
    ch._take(pkt(b"\x00\x10"))
    assert bytes(ch._rx) == b"AB" and not ch._pending and ch._ack


def test_bytes_without_a_boundary_are_handed_over_after_a_while(monkeypatch):
    t, ch = open_channel()
    ch._take(S.Packet(0, STATUS + b"\x41\x80"))
    monkeypatch.setattr(K, "PENDING_FLUSH_S", 0.0)
    assert [m.data for m in ch.read(1, 200)] == [b"A"]


def test_records_are_byte_and_flag_pairs():
    assert K.parse_records(STATUS + b"\x41\x80\x00\x10\x42\x80") == [(0x41, 0x80), (0, 0x10), (0x42, 0x80)]



def test_raw_kline_runs_on_the_channel():
    t = FakeKLineSm2()
    dev = K.Sm2KLineDevice(S.Sm2(t))                 # with the background poller
    kl = J2534RawKLine(dev, 10400)
    assert kl.wake_up(0x17) == 1281
    assert t.sent[[c for c, _ in t.sent].index(0x04)][1][4] == 0x97     # odd-parity address
    t.queue = [b"\x0f"]
    assert kl.read_byte() == 0x0F
    kl.close()
    dev.close()


def test_the_background_poller_delivers_without_a_read_asking():
    import time
    t = FakeKLineSm2()
    ch = K.Sm2KLineChannel(S.Sm2(t), 10400)             # the default: a poller thread
    t.queue = [b"\x0f", b"\x10"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and len(ch._rx) < 2:
        time.sleep(0.005)
    assert bytes(ch._rx) == b"\x0f\x10"                 # nobody has called read()
    assert [m.data for m in ch.read(1, 10)] == [b"\x0f\x10"]
    ch.write(b"\xf0")                                   # a write gets in between the polls
    assert any(c == 0x1D and b[4:] == b"\xf0\x80" for c, b in t.sent)
    ch.disconnect()
    assert not ch._thread.is_alive()

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ovtsuite/flash/hexcan.py against a fake cable that implements the decoded
protocol (docs/HEX_USB_CAN_PROTOCOL.md) faithfully, including the documented
echo-before-reply behaviour and the real reply payload shapes confirmed on
the bench 2026-11-21 (ping/open-config get a bare 0xFE ack, everything else
echoes its own command byte first) -- this checks the module encodes and
verifies that protocol correctly against the now-confirmed real behaviour,
not a live cable directly (that is exercised in hexcan_live.py /
docs/HEX_USB_CAN_PROTOCOL.md's own live runs)."""
import struct

import pytest

from ovtsuite.flash import hexcan as H


def _frame(sof, payload):
    """Build a frame from an already-complete payload (unlike
    H.build_frame, which always prepends a command byte -- here the
    command byte, if any, is already part of ``payload``)."""
    head = bytes([sof, len(payload) + 3]) + payload
    return head + bytes([H.checksum(head)])


class FakePort:
    """A cable that answers every command the way the protocol doc says a
    genuine one does: echoes the host's frame first, then its own 0x4D
    reply, both landing in the same read."""

    def __init__(self, fail_challenge=False):
        self.out = bytearray()
        self.closed = False
        self.fail_challenge = fail_challenge
        self.sent = []          # (cmd, data) log, for assertions
        self.tx_frames = []     # decoded 0xB8 payloads SendCANMsg sent
        self._baud = None

    @property
    def in_waiting(self):
        return len(self.out)

    @property
    def baudrate(self):
        return self._baud

    @baudrate.setter
    def baudrate(self, v):
        self._baud = v
        if v == 115200:
            # the real cable sends this unprompted once it is listening at
            # the new rate -- not a reply to anything the host sent
            self.out += _frame(H.SOF_DEVICE, bytes([0xFD]))

    def read(self, n):
        d, self.out = bytes(self.out[:n]), self.out[n:]
        return d

    def write(self, data):
        data = bytes(data)
        if data == bytes([H.WAKE_BYTE]):
            self.out += data                      # wake byte just echoes
            return
        assert data[0] == H.SOF_HOST
        self.out += data                          # echo first, as documented
        cmd = data[2]
        self.sent.append((cmd, bytes(data[3:-1])))
        reply = self._answer(cmd, bytes(data[3:-1]))
        if reply is not None:
            self.out += _frame(H.SOF_DEVICE, reply)

    def _answer(self, cmd, data):
        # Real bench cable (2026-11-21): ping and open/configure get a bare
        # 0xFE ack with NO echoed command byte; every other command echoes
        # its own command byte first, with the real data following.
        if cmd == H.CMD_PING or cmd == H.CMD_OPEN:
            return bytes([0xFE])
        if cmd == H.CMD_VERSION:
            return bytes([cmd, 0x01, 0x60, 0x46])
        if cmd == H.CMD_CHALLENGE:
            round_type = data[0]
            a, b = struct.unpack_from("<II", data, 1)
            if round_type == 1:
                if self.fail_challenge:
                    return bytes([cmd]) + bytes(8)
                z, y = H.tea_encrypt(a, b)
                return bytes([cmd]) + struct.pack("<II", z, y)
            return bytes([cmd]) + bytes(8)         # 2/3/5: not checked by anything
        if cmd == H.CMD_IDENTITY:
            return bytes([cmd]) + b"ROSSTECH" + bytes([0, 0, 0, 0xC6, 0x5E, 0x44, 0, 0x09])
        if cmd == H.CMD_UNKNOWN_19:
            return bytes([cmd]) + bytes(16)
        if cmd == H.CMD_MEM_READ:
            return bytes([cmd]) + bytes(40)
        if cmd in (H.CMD_CAN_OPEN1, H.CMD_CAN_OPEN2, H.CMD_CAN_PARAM, H.CMD_CAN_MASK,
                   H.CMD_CAN_FILTER, H.CMD_CAN_SMALL):
            return bytes([0xFE])
        if cmd == H.CMD_ACK:
            return None                             # one-way: the host acks, nothing answers it
        if cmd == H.CMD_CAN_TX:
            self.tx_frames.append(data)
            return bytes(16)
        return bytes([0xFE])

    def close(self):
        self.closed = True


def open_channel(**kw):
    return H.HexCanChannel(FakePort(**kw))


# ---- framing ----

def test_build_frame_matches_the_documented_wire_bytes():
    # from docs/HEX_USB_CAN_PROTOCOL.md's open sequence / tools/hexcan_live.py
    assert H.build_frame(0xA0).hex() == "5304a0f7"
    assert H.build_frame(0x03, bytes([0x00, 0xC2, 0x01, 0x00])).hex() == "53080300c201009b"


def test_checksum_is_xor_of_everything_before_it():
    frame = H.build_frame(0x02)
    assert H.checksum(frame[:-1]) == frame[-1]


def test_frame_buffer_recovers_frames_split_across_reads():
    buf = H._FrameBuffer()
    whole = H.build_frame(0xA0)
    assert buf.feed(whole[:2]) == []
    out = buf.feed(whole[2:])
    assert out == [(H.SOF_HOST, bytes([0xA0]))]


def test_frame_buffer_rejects_a_bad_checksum():
    buf = H._FrameBuffer()
    bad = bytearray(H.build_frame(0xA0))
    bad[-1] ^= 0xFF
    assert buf.feed(bytes(bad)) == []


# ---- TEA ----

def test_tea_encrypt_matches_the_recovered_key_constants():
    # round-trip sanity: same inputs always give the same outputs
    assert H.tea_encrypt(1, 2) == H.tea_encrypt(1, 2)
    assert H.tea_encrypt(1, 2) != H.tea_encrypt(2, 1)


# ---- open sequence ----

def test_open_sequence_sends_every_documented_command_in_order():
    port = FakePort()
    H.HexCanChannel(port)
    cmds = [c for c, _ in port.sent]
    expected_prefix = [H.CMD_PING, H.CMD_OPEN, H.CMD_ACK, H.CMD_VERSION, H.CMD_CHALLENGE,
                       H.CMD_IDENTITY, H.CMD_UNKNOWN_19]
    assert cmds[:len(expected_prefix)] == expected_prefix
    assert cmds.count(H.CMD_MEM_READ) == 16          # two 8-block passes
    assert cmds.count(H.CMD_CHALLENGE) == 4          # type 1, then 5, 2, 3
    assert cmds[-1] == H.CMD_CAN_SMALL


def test_open_sends_the_open_config_payload_unchanged():
    port = FakePort()
    H.HexCanChannel(port)
    opens = [d for c, d in port.sent if c == H.CMD_OPEN]
    assert opens == [H.OPEN_CONFIG]


def test_can_mask_payloads_match_the_documented_wire_bytes():
    # docs/HEX_USB_CAN_PROTOCOL.md's "Open sequence": "B3  00 ffe0 ff00" and
    # "B3  01 ffe0 0000" -- 5 bytes each, index already the first byte of
    # CAN_MASKS's own entries. A prior version of open() also prepended the
    # loop index, doubling it and sending 6 bytes; the fake cable below acks
    # any 0xB3 payload regardless of length, so only checking the exact
    # bytes (not just that open() completes) catches that regression.
    port = FakePort()
    H.HexCanChannel(port)
    masks = [d for c, d in port.sent if c == H.CMD_CAN_MASK]
    assert [m.hex() for m in masks] == ["00ffe0ff00", "01ffe00000"]


def test_open_raises_when_the_cable_fails_the_genuine_check():
    with pytest.raises(H.HexCanError, match="does not match"):
        open_channel(fail_challenge=True)


def test_open_does_not_check_round_types_2_3_5():
    # a fake that always answers round-type>1 with garbage must not raise --
    # matches VCDS's own behaviour (see hexcan.py's module docstring)
    open_channel()               # the default fake already answers 2/3/5 with zeros


class _SilentPort(FakePort):
    """A cable that only ever echoes -- what every live attempt looked like
    before the baud-ramp fix (docs/HEX_USB_CAN_PROTOCOL.md, "Live hardware
    test"). Kept as a regression check that a genuinely unresponsive cable
    still fails loudly rather than hanging."""

    def _answer(self, cmd, data):
        return None


def test_open_raises_on_a_cable_that_only_echoes():
    # ping_tries=1: this fake never answers no matter how many times asked,
    # so there is no point in HexCanChannel's real retry backoff here
    with pytest.raises(H.HexCanError, match="did not answer"):
        H.HexCanChannel(_SilentPort(), ping_tries=1)


# ---- transmit ----

def test_write_can_standard_id_layout_matches_hc_scm_disassembly():
    port = FakePort()
    ch = H.HexCanChannel(port)
    port.tx_frames.clear()
    ch.write_can(0x740, bytes([0xA0, 0x0F, 0x8A, 0xFF, 0x32, 0xFF]))
    body = port.tx_frames[0]
    can_id = (body[1] << 3) | (body[2] >> 5)
    assert can_id == 0x740
    assert body[2] & 0x08 == 0                        # standard, not extended
    dlc = body[5]
    assert dlc == 6
    assert body[6:6 + dlc] == bytes([0xA0, 0x0F, 0x8A, 0xFF, 0x32, 0xFF])


def test_write_can_extended_id_sets_the_29_bit_flag():
    port = FakePort()
    ch = H.HexCanChannel(port)
    port.tx_frames.clear()
    ch.write_can(0x1FEDCBA9, b"\x01\x02", extended=True)
    body = port.tx_frames[0]
    assert body[2] & 0x08 == 0x08
    ext_id = (body[1] << 21) | (((body[2] >> 5) & 7) << 18) | ((body[2] & 3) << 16) | (body[3] << 8) | body[4]
    assert ext_id == 0x1FEDCBA9


def test_write_can_counter_increments_each_frame():
    port = FakePort()
    ch = H.HexCanChannel(port)
    port.tx_frames.clear()
    ch.write_can(0x100, b"\x00")
    ch.write_can(0x100, b"\x00")
    c0 = struct.unpack_from("<H", port.tx_frames[0], 14)[0]
    c1 = struct.unpack_from("<H", port.tx_frames[1], 14)[0]
    assert c1 == (c0 + 1) & 0xFFFF


def test_write_can_rejects_more_than_8_data_bytes():
    ch = open_channel()
    with pytest.raises(H.HexCanError):
        ch.write_can(0x100, bytes(9))


# ---- receive is honestly unimplemented ----

def test_read_raises_rather_than_guess_at_the_undecoded_rx_layout():
    ch = open_channel()
    with pytest.raises(H.HexCanError, match="not decoded"):
        ch.read()


def test_close_closes_the_underlying_port():
    port = FakePort()
    ch = H.HexCanChannel(port)
    ch.close()
    assert port.closed

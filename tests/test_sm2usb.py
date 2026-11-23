# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SM2 over USB (ovtsuite/flash/sm2usb.py): framing, stream resync, the ping /
info handshake, event queueing, and the refusal to send firmware-update
commands -- all against a fake device, no hardware."""
import struct

import pytest

from ovtsuite.flash import sm2usb as S


class FakeSm2:
    """Two-method transport that answers like the firmware: ping echoes, info
    returns a canned record, anything else is answered by ``replies``."""

    def __init__(self, build=0x0A111788, model=2, replies=None, chunk=64, stale=b""):
        self.sent = []
        self.out = bytearray(stale)
        self.build, self.model = build, model
        self.replies = replies or {}
        self.chunk = chunk
        self.buf = bytearray()
        self.closed = False

    def info_payload(self):
        p = bytearray(0x52)
        struct.pack_into("<I", p, 0, self.build)
        p[16] = self.model
        p[18:18 + 7] = b"SM2 PRO"
        p[34:34 + 7] = b"A111788"
        p[50:50 + 6] = b"2.21.3"
        return bytes(p)

    def write(self, data):
        self.buf += data
        while len(self.buf) >= 4:
            cmd, n = self.buf[0], self.buf[1] | (self.buf[2] << 8)
            assert self.buf[3] == S.checksum(cmd, n), "host sent a bad header"
            if len(self.buf) < 4 + n:
                return
            body = bytes(self.buf[4:4 + n])
            del self.buf[:4 + n]
            self.sent.append((cmd, body))
            if cmd == S.CMD_PING:
                self.out += S.pack(cmd, body)
            elif cmd == S.CMD_INFO:
                self.out += S.pack(0, self.info_payload())
            elif cmd in self.replies:
                r = self.replies[cmd]
                self.out += r(body) if callable(r) else r

    def read(self, timeout_ms):
        if not self.out:
            return b""
        data = bytes(self.out[:self.chunk])
        del self.out[:self.chunk]
        return data

    def close(self):
        self.closed = True


def test_header_matches_what_the_vendor_dll_sends():
    # captured from smj2534.dll: ping with a 4-byte clock, then the info request
    assert S.pack(0x86, bytes.fromhex("2b87f0c1")).hex() == "860400df2b87f0c1"
    assert S.pack(0x83).hex() == "830000d8"
    # and a reply the device sends: status 0, 0x52 bytes
    assert S.checksum(0x00, 0x52) == 0xA7


def test_stream_splits_frames_across_usb_packets_and_resyncs():
    st = S.Stream()
    f1, f2 = S.pack(0, b"hello"), S.pack(0xFE, b"\x01\x02")
    junk = b"\x99\x98"                                     # not a valid header
    blob = junk + f1 + f2
    got = []
    for i in range(0, len(blob), 3):                        # trickle in 3 bytes at a time
        st.feed(blob[i:i + 3])
        while (p := st.next()) is not None:
            got.append(p)
    assert [(p.status, p.payload) for p in got] == [(0, b"hello"), (0xFE, b"\x01\x02")]
    assert st.skipped == 2


def test_handshake_reads_info_and_labels_the_build():
    t = FakeSm2()
    with S.Sm2(t) as dev:
        assert dev.info.build_name == "A111788"
        assert dev.info.model == 2
        assert dev.info.strings[:3] == ["SM2 PRO", "A111788", "2.21.3"]
        assert "A111788" in dev.label
    assert [c for c, _ in t.sent] == [S.CMD_PING, S.CMD_INFO]
    assert t.closed


def test_reply_longer_than_one_usb_packet_is_reassembled():
    # the info reply is 86 bytes: two 64-byte bulk transfers
    t = FakeSm2(chunk=64)
    assert S.Sm2(t).info.raw[:4] == struct.pack("<I", 0x0A111788)


@pytest.mark.parametrize("build", [0xA78730, 0xA114969, 0xA111788, 0xA9890])
def test_every_known_build_is_accepted(build):
    # the vendor DLL keeps a whitelist of builds (and re-flashes anything else);
    # this driver must work whatever firmware is on the device
    assert S.Sm2(FakeSm2(build=build)).info.build == build


def test_stale_bytes_from_a_previous_session_are_flushed():
    t = FakeSm2(stale=S.pack(0, b"leftover reply") + b"\x07\x07")
    dev = S.Sm2(t)
    assert dev.info is not None


def test_events_are_queued_not_returned_as_replies():
    t = FakeSm2(replies={0x1C: lambda b: S.pack(0xFE, b"async") + S.pack(0, b"answer")})
    dev = S.Sm2(t)
    pkt = dev.command(0x1C)
    assert pkt.payload == b"answer"
    assert [e.payload for e in dev.events] == [b"async"]


def test_non_zero_status_is_returned_not_raised():
    t = FakeSm2(replies={0x1D: lambda b: S.pack(0x09, b"")})
    pkt = S.Sm2(t).command(0x1D)
    assert pkt.status == 9 and not pkt.ok


def test_no_reply_is_an_error():
    dev = S.Sm2(FakeSm2())
    with pytest.raises(S.Sm2Error, match="no reply"):
        dev.command(0x1E, timeout_ms=20)


@pytest.mark.parametrize("cmd", sorted(S.FIRMWARE_UPDATE_CMDS))
def test_firmware_update_commands_are_never_sent(cmd):
    t = FakeSm2()
    dev = S.Sm2(t)
    n = len(t.sent)
    with pytest.raises(S.Sm2Error, match="firmware-update"):
        dev.command(cmd, b"\x00" * 36)
    with pytest.raises(S.Sm2Error):
        dev.send(cmd)
    assert len(t.sent) == n


def test_a_ping_that_is_not_echoed_fails():
    t = FakeSm2()
    real_write = t.write

    def write(data):
        real_write(data)
        if data[0] == S.CMD_PING:
            t.out[:] = S.pack(0x86, b"\0\0\0\0")             # wrong token
    t.write = write
    with pytest.raises(S.Sm2Error, match="ping"):
        S.Sm2(t)


def test_unsupported_model_code_is_refused():
    with pytest.raises(S.Sm2Error, match="unsupported"):
        S.Sm2(FakeSm2(model=4))


def test_info_parse_tolerates_short_and_odd_replies():
    assert S.Info.parse(b"").build == 0
    i = S.Info.parse(struct.pack("<I", 0xA78730))
    assert i.build_name == "A78730" and i.strings == []


def test_firmware_command_table_has_the_42_dispatched_commands():
    assert len(S.FIRMWARE_COMMANDS) == 42
    assert S.CMD_PING not in S.FIRMWARE_COMMANDS and S.CMD_INFO not in S.FIRMWARE_COMMANDS
    assert not (set(S.FIRMWARE_COMMANDS) & S.FIRMWARE_UPDATE_CMDS)


def test_device_ids_match_the_firmware_descriptor():
    # device descriptor in all three images: idVendor 0x20A2, idProduct 0x0001
    assert (S.VID, S.PID) == (0x20A2, 0x0001)


def test_battery_voltage_reads_the_low_word_in_millivolts():
    # captured from a real SM2 Pro (A111788): the DLL's READ_VBATT said 5391 mV
    t = FakeSm2(replies={S.CMD_VBATT: S.pack(0, bytes.fromhex("0f15bf00"))})
    assert S.Sm2(t).battery_voltage() == pytest.approx(5.391)
    assert S.Sm2(FakeSm2()).battery_voltage() is None          # no reply: None, not an error

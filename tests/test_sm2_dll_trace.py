# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""tools/sm2_dll_trace.py: the parts that run without a device -- the IAT lookup
against the real Scanmatik DLL, the safety rewrite of the device-info reply,
and the socket bridge."""
import os
import socket
import struct
import threading

import pytest

from tools import sm2_dll_trace as T

DLL = T.DEFAULT_DLL


@pytest.mark.skipif(not os.path.isfile(DLL), reason="Reference/Driver/smj2534.dll not present")
def test_iat_slots_of_the_scanmatik_dll():
    slots = T._import_slots(DLL, "KERNEL32.dll", ("CreateFileW", "ReadFile", "WriteFile", "DeviceIoControl"))
    # read from the DLL's import table with a disassembler: call [0x100a31d0] etc.
    assert slots == {"CreateFileW": 0xA31D0, "ReadFile": 0xA31B0, "WriteFile": 0xA31BC,
                     "DeviceIoControl": 0xA3234}


def _info(build):
    p = bytearray(0x52)
    struct.pack_into("<I", p, 0, build)
    return bytearray(T.frame(0, bytes(p)))


def test_unlisted_build_is_hidden_from_the_dll_so_it_does_not_reflash():
    buf = _info(0xA111788)                       # SM2 Pro 2.21.32: not on the DLL's list
    assert T.rewrite_info_reply(buf)
    assert struct.unpack_from("<I", buf, 4)[0] == T.SPOOF_BUILD
    assert T.SPOOF_BUILD in T.DLL_BUILDS
    assert buf[3] == T.chk(0, 0x52)              # header untouched, still valid


def test_the_first_usb_packet_of_the_reply_is_enough():
    full = _info(0xA111788)
    first = bytearray(full[:64])                 # a bulk transfer carries at most 64 bytes
    assert T.rewrite_info_reply(first)
    assert struct.unpack_from("<I", first, 4)[0] == T.SPOOF_BUILD


def test_listed_builds_and_other_replies_are_left_alone():
    for b in T.DLL_BUILDS:
        buf = _info(b)
        assert not T.rewrite_info_reply(buf)
    short = bytearray(T.frame(0, b"\x01\x02\x03\x04"))
    assert not T.rewrite_info_reply(short)
    err = bytearray(T.frame(9, bytes(0x52)))     # an error status is not an info record
    assert not T.rewrite_info_reply(err)


def test_update_commands_are_the_four_the_dll_uses():
    assert set(T.FW_UPDATE) == {0x80, 0x81, 0x82, 0x8B}


def test_bridge_carries_writes_and_reads():
    class Dev:
        def __init__(self):
            self.written, self.queue = [], [b"abc", b""]

        def write(self, d):
            self.written.append(bytes(d))

        def read(self, ms):
            return self.queue.pop(0) if self.queue else b""

    a, b = socket.socketpair()
    dev = Dev()
    th = threading.Thread(target=T._bridge, args=(a, dev), daemon=True)
    th.start()
    br = T.Bridge.__new__(T.Bridge)
    br.s, br.lock = b, threading.Lock()
    br.write(b"\x86\x04\x00\xdf1234")
    assert br.read(50) == b"abc"
    assert br.read(50) == b""
    b.close()
    th.join(2)
    assert dev.written == [b"\x86\x04\x00\xdf1234"]

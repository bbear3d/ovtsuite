# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The K-line 09D sim after a 5-baud init: the Scanmatik interface may or may
not drive ~KB2 on the wire, and the wire may or may not echo our TX; either
way the tester's first request must be answered (the bench shim showed
unanswered ``82 1A F1 1A 9B``)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ovtsuite.flash import trx_kline_sim as T
from ovtsuite.flash.transport import KLineTimeout


class Port:
    def __init__(self, incoming):
        self.rx = bytearray(incoming)
        self.out = bytearray()
        self.read_timeout = 1.0
    def read_byte(self):
        if not self.rx:
            raise KLineTimeout("t")
        return self.rx.pop(0)
    def read(self, n):
        if len(self.rx) < n:
            self.rx.clear()
            raise KLineTimeout("t")
        o = bytes(self.rx[:n]); del self.rx[:n]; return o
    def write_bytes(self, b):
        self.out += bytes(b)
    def clear_rx(self):
        pass
    def set_baud(self, b):
        pass


class EchoPort(Port):
    """A shared wire: our own bytes come straight back."""
    def write_bytes(self, b):
        super().write_bytes(b)
        self.rx[0:0] = bytes(b)


class Resp:
    def handle(self, payload):
        return b"\x5A\x9B" + b"X" if payload[:2] == b"\x1A\x9B" else None


REQ = bytes([0x82, 0x1A, 0xF1, 0x1A, 0x9B])
REQ += bytes([sum(REQ) & 0xFF])
ANSWER = b"\x5A\x9B\x58"


def _run(first_bytes, echo=False, port_cls=Port):
    T.SLOW_INIT_WAIT_S = 0.0
    p = port_cls(bytes([0x00]) + first_bytes + REQ)
    T.KLineEcu(p, Resp(), echo=echo).run()
    return bytes(p.out)


def test_request_answered_when_tester_sends_inverted_kb2():
    out = _run(bytes([0x70]))
    assert out.startswith(bytes([0x55, 0x6B, 0x8F, 0xFD]))
    assert ANSWER in out


def test_request_answered_when_no_inversion_is_sent():
    out = _run(b"")
    assert out.startswith(bytes([0x55, 0x6B, 0x8F]))
    assert out[3] == 0x83 and ANSWER in out


def test_no_echo_on_the_wire_does_not_eat_the_request():
    # bench 2026-11-15: the wire did not echo our TX; a blind "swallow N bytes"
    # ate ~KB2 and the first two request bytes and the tool got no answer.
    out = _run(bytes([0x70]), echo=True)
    assert out.startswith(bytes([0x55, 0x6B, 0x8F, 0xFD]))
    assert ANSWER in out


def test_echo_is_still_swallowed():
    out = _run(bytes([0x70]), echo=True, port_cls=EchoPort)
    assert out.startswith(bytes([0x55, 0x6B, 0x8F, 0xFD]))
    assert ANSWER in out


def test_the_erase_reply_is_held_with_response_pending():
    class R:
        erase_delay_s, pending_interval_s, erase_pending = 0.3, 0.1, True
        def is_erase(self, p):
            return p[:2] == bytes([0x31, 0xC4])
        def handle(self, p):
            return bytes([0x71, 0xC4]) if self.is_erase(p) else None
    f = bytes([0x82, 0x1A, 0xF1, 0x31, 0xC4])
    p = Port(f + bytes([sum(f) & 0xFF]))
    e = T.KLineEcu(p, R(), echo=False)
    t0 = __import__("time").monotonic()
    e.do_init = lambda *a, **k: True
    e.run()
    waited = __import__("time").monotonic() - t0
    out = bytes(p.out)
    assert out.count(bytes([0x7F, 0x31, 0x78])) >= 2, out.hex(" ")
    last = out[-6:]
    assert last[3:5] == bytes([0x71, 0xC4]) and last[5] == sum(last[:5]) & 0xFF and waited >= 0.3

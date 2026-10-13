# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Serial K-line fast init: the hardware-timed Tinil pulse (0x00 @ 360 baud) and
the retry-before-fallback behaviour.  No hardware -- a fake port records the baud
changes / breaks / writes and feeds back a StartCommunication reply."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite.flash.transport import (          # noqa: E402
    KLine, KLineError, _FAST_INIT_TRIES)


class FakePort:
    """Minimal pyserial-shaped port: half-duplex echo, and an ECU that answers a
    StartCommunication (81 ..) with 83 F1 10 C1 EF 8F <cks> (keyword EF 8F)."""

    def __init__(self, answer_sc=True):
        self.baudrate = 10400
        self.break_condition = False
        self.timeout = 8.0
        self.dtr = False
        self.rts = False
        self.answer_sc = answer_sc
        self.events = []
        self._rx = bytearray()

    def write(self, data):
        data = bytes(data)
        self.events.append(("write", data))
        self._rx += data                          # K-line half-duplex self-echo
        if self.answer_sc and data[:1] == b"\x81":
            resp = bytes([0x83, 0xF1, 0x10, 0xC1, 0xEF, 0x8F])
            self._rx += resp + bytes([sum(resp) & 0xFF])
        return len(data)

    def flush(self):
        pass

    def read(self, n):
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def reset_input_buffer(self):
        self._rx.clear()

    # KLine sets these via properties; plain attributes suffice
    def __setattr__(self, k, v):
        if hasattr(self, "events"):
            if k == "baudrate":
                self.events.append(("baud", v))
            elif k == "break_condition":
                self.events.append(("break", bool(v)))
        object.__setattr__(self, k, v)


def _kline(port, direct=True):
    k = KLine.__new__(KLine)
    k._timeout = 2.0
    k._trace = None
    k.prefer_fast_init = True
    k.direct = direct
    k.port = port
    k.last_sync = -1
    return k


def test_fast_init_break_pulse_and_reply_scan():
    port = FakePort()
    k = _kline(port, direct=True)
    version = k._fast_init(0x01)
    assert version == 2031                          # keyword EF 8F -> KW2031

    # the TiniL low is a break pulse (asserted then released), NOT a 0x00 byte
    breaks = [v for (kind, v) in port.events if kind == "break"]
    assert True in breaks and False in breaks
    writes = [v for (kind, v) in port.events if kind == "write"]
    assert b"\x00" not in writes                     # no 0x00-at-360 pulse byte
    # StartCommunication targets the KWP controller 0x10: 81 10 F1 81 + cks
    assert any(w[:4] == bytes([0x81, 0x10, 0xF1, 0x81]) for w in writes)
    # the reply is found by SCANNING the burst for 0xC1, so the leading echo
    # (81 10 F1 81 03, no 0xC1) never has to be drained to a byte boundary


def test_fast_init_retries_then_falls_back_to_slow():
    calls = {"fast": 0, "slow": 0}

    def fake_fast(addr):
        calls["fast"] += 1
        raise KLineError("miss")

    def fake_slow(addr, even_parity):
        calls["slow"] += 1
        return 2031

    k = _kline(FakePort(answer_sc=False), direct=True)
    k._fast_init = fake_fast
    k._wake_up_once = fake_slow
    assert k.wake_up() == 2031
    assert calls["fast"] == _FAST_INIT_TRIES        # fast tried every attempt
    assert calls["slow"] == 1                        # then fell back to 5-baud slow init


def test_fast_init_raises_when_ecu_silent():
    k = _kline(FakePort(answer_sc=False), direct=True)
    try:
        k._fast_init(0x01)
    except KLineError:
        pass
    else:
        raise AssertionError("expected KLineError on a silent ECU")

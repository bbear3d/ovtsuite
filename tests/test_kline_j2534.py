# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""J2534 K-line link: the same EDC16 read/write routine the serial K-line runs,
but connected with hardware fast init and carried over a J2534 ISO 14230 channel.

Reuses the full EDC16 wire-protocol mock from test_flash_edc16 (MockEcu), wrapped
as a J2534 device: a written frame goes in as one ISO 14230 message, the ECU's
reply comes back as one message, and the device filters the half-duplex TX echo
(so J2534KLine synthesizes it only where the serial port would have shown it)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import edc16 as E          # noqa: E402
from ovtsuite.flash import j2534 as J          # noqa: E402
from ovtsuite.flash.kline_j2534 import J2534KLine  # noqa: E402
from test_flash_edc16 import MockEcu             # noqa: E402

import pytest

#: Slow: drives the K-line fast-init sequence including its real waits.
pytestmark = pytest.mark.slow



class MockJ2534Channel:
    """ISO 14230 channel over a MockEcu.  write() sends one KWP frame (device
    strips the echo); read() returns the ECU's reply as one Msg."""

    def __init__(self, ecu, protocol):
        self.ecu = ecu
        self.protocol = protocol
        self.fast_init_count = 0
        self.slow_init_count = 0
        self.baud = 10400

    def pass_all(self, extended=False):
        pass

    def set_config(self, **kw):
        pass

    def set_baud(self, b):
        self.baud = b
        self.ecu.baudrate = b

    def clear_rx(self):
        self.ecu.rx.clear()

    def fast_init(self, request):
        self.fast_init_count += 1
        # StartCommunication positive response + keyword EF 8F (KW2031), as the
        # real captures show (83 F1 10 C1 EF 8F C3)
        return bytes([0x83, 0xF1, 0x10, 0xC1, 0xEF, 0x8F, 0xC3])

    def five_baud_init(self, address):
        self.slow_init_count += 1
        return bytes([0xEF, 0x8F])

    def write(self, frame, flags=0, timeout_ms=100):
        frame = bytes(frame)
        self.ecu.write(frame)                    # echoes + generates the reply into ecu.rx
        del self.ecu.rx[:len(frame)]             # device filters the half-duplex TX echo
        self._indicated = False                  # emit a START_OF_MESSAGE indication before data

    def read(self, max_msgs=1, timeout_ms=100):
        if not self.ecu.rx:
            return []
        if not getattr(self, "_indicated", True):
            # real J2534 devices precede a message with a zero-length indication
            # frame; the link must skip it, not treat it as the reply
            self._indicated = True
            return [J.Msg(self.protocol, J.START_OF_MESSAGE, 0, 0, b"")]
        data = bytes(self.ecu.rx)
        self.ecu.rx.clear()
        return [J.Msg(self.protocol, 0, 0, 0, data)]

    def disconnect(self):
        pass


class MockJ2534Dev:
    def __init__(self, ecu):
        self.ecu = ecu
        self.channels = []

    def connect(self, protocol, baud, flags=0):
        ch = MockJ2534Channel(self.ecu, protocol)
        self.channels.append(ch)
        return ch


def _make(cal=None):
    ecu = MockEcu(cal)
    dev = MockJ2534Dev(ecu)
    link = J2534KLine(dev)
    return E.Edc16Flash(link), ecu, dev, link


def test_j2534_kline_read_uses_fast_init_and_roundtrips():
    cal = bytes(((i * 7 + 3) & 0xFF) for i in range(E.CAL_SIZE))
    flash, ecu, dev, link = _make(bytearray(cal))
    img = flash.read_flash(speed="Low", allow_fast=False)     # K-line read path (no chunking)
    assert len(img) == E.FULL_SIZE
    assert img[:E.CAL_START] == b"\xFF" * E.CAL_START
    assert img[E.CAL_START:] == cal
    # connected via hardware FAST init, not 5-baud slow init
    assert dev.channels[0].fast_init_count >= 1
    assert dev.channels[0].slow_init_count == 0
    # ISO 14230 channel, no checksum (we build/verify our own)
    assert dev.channels[0].protocol == J.ISO14230


def test_j2534_kline_write_then_read_roundtrips():
    flash_w, ecu, dev, link = _make(bytearray(b"\x22" * E.CAL_SIZE))
    image = bytearray(b"\xFF" * E.FULL_SIZE)
    for i in range(E.CAL_SIZE):
        image[E.CAL_START + i] = (i * 13 + 5) & 0xFF
    flash_w.write_flash(image, force_full=True, speed="Low", allow_fast=False)
    written = bytes(ecu.flash)
    assert written[:0x40000] == bytes(image[0x180000:0x1C0000])                    # block6
    assert written[0x40000:0x40000 + 0x3E000] == bytes(image[0x1C0000:0x1FE000])   # block7
    # WRITE uses SLOW (5-baud) init by default: the EDC16 grants the 0x85 download
    # session only over a slow-init session (this mock ECU accepts a cold slow init,
    # so no fast-init prime is needed).
    assert dev.channels[0].slow_init_count >= 1
    assert dev.channels[0].fast_init_count == 0


def test_j2534_kline_discovers_high_baud_fresh_init_per_candidate():
    # the device rate matching the ECU's High baud is unknown and can't be found
    # by cycling rates on a live session; edc16 tries each candidate with a FRESH
    # fast init and keeps the first that verifies (1A 9C -> clean 5A 9C).  The
    # mock ECU only answers at the "correct" high rate (or the 10400 init rate).
    from ovtsuite.flash.kline_j2534 import J2534KLine, _DISCOVERED_HIGH_BAUD
    from ovtsuite.flash.edc16 import SPEEDS
    _DISCOVERED_HIGH_BAUD.clear()
    E.HIGH_BAUD_SETTLE_S = 0.0                                # skip the real inter-candidate wait
    CORRECT = 244800                                          # a candidate partway down the list

    class BaudAwareChannel(MockJ2534Channel):
        def write(self, frame, flags=0, timeout_ms=100):
            f = bytes(frame)
            if f[:4] == b"\x83\x10\xf1\x10" and len(f) >= 6:  # fast session-enter: ack at the init rate
                resp = bytes([0x83, 0xF1, 0x10, 0x50, f[4], f[5]])
                self.ecu.rx += resp + bytes([sum(resp) & 0xFF])
                self._indicated = False
                return
            if self.baud <= 38400 or self.baud == CORRECT:    # right rate (or init rate) -> ECU hears it
                super().write(f, flags, timeout_ms)
            # a wrong high rate feeds the ECU garbage -> no usable reply (dropped)

    class BaudAwareDev(MockJ2534Dev):
        def connect(self, protocol, baud, flags=0):
            ch = BaudAwareChannel(self.ecu, protocol); self.channels.append(ch); return ch

    ecu = MockEcu(bytearray(b"\x11" * E.CAL_SIZE))
    dev = BaudAwareDev(ecu)
    link = J2534KLine(dev)
    flash = E.Edc16Flash(link)

    def prepare():
        flash.connect()
        flash._lvl3_auth()
    flash._establish_session(0x86, "High", True, prepare)     # runs the fresh-init discovery sweep
    assert _DISCOVERED_HIGH_BAUD.get(SPEEDS["High"][0]) == CORRECT
    assert link._ch.baud == CORRECT                           # left in session at the discovered rate
    assert sum(c.fast_init_count for c in dev.channels) >= 3  # a fresh init per failed candidate + winner
    # cached now: a second establish uses the cached rate without re-sweeping
    n = len(dev.channels)
    flash._establish_session(0x86, "High", True, prepare)
    assert len(dev.channels) - n <= 2                         # one prepare, no full sweep
    link.close()


def test_j2534_kline_high_falls_back_to_medium_not_10400():
    # When no High device rate verifies, discovery must drop to Medium (124800),
    # which has a clean divisor -- NOT to 10400 (12x slower).  Mock verifies at no
    # High candidate but acks the session-enter, so the Medium fast path takes.
    from ovtsuite.flash.kline_j2534 import _DISCOVERED_HIGH_BAUD
    from ovtsuite.flash.edc16 import SPEEDS
    _DISCOVERED_HIGH_BAUD.clear()
    E.HIGH_BAUD_SETTLE_S = 0.0
    MED = SPEEDS["Medium"][1]                                 # 124800

    class NoHighChannel(MockJ2534Channel):
        def write(self, frame, flags=0, timeout_ms=100):
            f = bytes(frame)
            if f[:4] == b"\x83\x10\xf1\x10" and len(f) >= 6:  # ack any session-enter
                resp = bytes([0x83, 0xF1, 0x10, 0x50, f[4], f[5]])
                self.ecu.rx += resp + bytes([sum(resp) & 0xFF])
                self._indicated = False
                return
            if self.baud <= MED:                             # 10400/38400/124800 heard; High rates deaf
                super().write(f, flags, timeout_ms)

    class NoHighDev(MockJ2534Dev):
        def connect(self, protocol, baud, flags=0):
            ch = NoHighChannel(self.ecu, protocol); self.channels.append(ch); return ch

    ecu = MockEcu(bytearray(b"\x11" * E.CAL_SIZE))
    dev = NoHighDev(ecu)
    link = J2534KLine(dev)
    link.high_baud_candidates = lambda: [250000, 245000, 240000]   # short sweep, all deaf
    flash = E.Edc16Flash(link)

    def prepare():
        flash.connect()
        flash._lvl3_auth()
    flash._establish_session(0x86, "High", True, prepare)
    assert link._ch.baud == MED                              # landed on Medium, not 10400
    assert _DISCOVERED_HIGH_BAUD.get(SPEEDS["High"][0]) is None   # nothing cached (no High match)
    link.close()


def test_j2534_kline_discovery_is_cancellable():
    # Cancel must be honoured during the fresh-init baud sweep, not only once the
    # transfer has started -- otherwise the UI's Cancel does nothing while it hunts.
    from ovtsuite.flash.kline_j2534 import _DISCOVERED_HIGH_BAUD
    _DISCOVERED_HIGH_BAUD.clear()
    E.HIGH_BAUD_SETTLE_S = 0.0
    ecu = MockEcu(bytearray(b"\x11" * E.CAL_SIZE))
    dev = MockJ2534Dev(ecu)                       # plain mock: no rate ever verifies
    link = J2534KLine(dev)
    flash = E.Edc16Flash(link)

    def prepare():
        flash.connect()
        flash._lvl3_auth()
    import pytest as _pytest
    with _pytest.raises(E.StopRequested):
        flash._establish_session(0x86, "High", True, prepare, is_stop=lambda: True)
    link.close()


def test_j2534_kline_slow_fallback_when_fast_disabled():
    cal = bytes(((i * 3) & 0xFF) for i in range(E.CAL_SIZE))
    ecu = MockEcu(bytearray(cal))
    dev = MockJ2534Dev(ecu)
    link = J2534KLine(dev, fast_init=False)                    # force 5-baud init
    ver = link.wake_up()
    assert ver >= 2000
    assert dev.channels[0].slow_init_count == 1 and dev.channels[0].fast_init_count == 0
    link.close()

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


def test_five_baud_key_bytes_read_from_the_front():
    from ovtsuite.flash.kline_j2534 import _version_from_keybytes
    # a Scanmatik hands back a padded 4-byte buffer: 01 8A 00 00 is KW1281
    assert _version_from_keybytes(bytes([0x01, 0x8A, 0x00, 0x00])) == 1281
    assert _version_from_keybytes(bytes([0xEF, 0x8F])) == 2031
    assert _version_from_keybytes(bytes([0x55, 0xEF, 0x8F])) == 2031


def test_set_baud_before_the_channel_opens_is_kept():
    from ovtsuite.flash.kline_j2534 import J2534KLine
    k = J2534KLine(object(), fast_init=False)
    k.set_baud(9600)
    assert k._baud0 == 9600


class _SplitChannel:
    """An ISO9141 channel with a short P1_MAX: each reply arrives in pieces,
    after the device's TX echo; records its config and writes."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.config = {}
        self.queue = []
        self.writes = []

    def set_config(self, **kw):
        self.config.update(kw)

    def write(self, data, flags=0, timeout_ms=100):
        self.writes.append(bytes(data))
        self.queue.append(J.Msg(J.ISO9141, J.TX_MSG_TYPE, 0, 0, bytes(data)))
        for part in self.replies.pop(0):
            self.queue.append(J.Msg(J.ISO9141, 0, 0, 0, part))

    def read(self, max_msgs=1, timeout_ms=100):
        out, self.queue = self.queue[:max_msgs], self.queue[max_msgs:]
        return out

    def clear_rx(self):
        self.queue = []


def _link_on(ch):
    k = J2534KLine(object(), fast_init=False)
    k._ch = ch
    k._timeout = 0.5
    return k


def test_session_timing_drops_the_init_waits():
    ch = _SplitChannel([])
    _link_on(ch).set_session_timing()
    assert ch.config == {"P4_MIN": 0, "P3_MIN": 0, "P1_MAX": 2}


def test_a_reply_in_pieces_is_put_back_together():
    ch = _SplitChannel([[bytes.fromhex("0001"), bytes.fromhex("7677")]])
    k = _link_on(ch)
    k.write_bytes(bytes.fromhex("02367FB7"))
    assert k.read(4) == bytes.fromhex("00017677")


def test_kline_exchange_is_used_and_pending_is_not_resent():
    # the channel's one-call exchange carries the request; a responsePending
    # reply is waited past without sending anything again
    class _X(_SplitChannel):
        def kline_exchange(self, frame, write_timeout_ms=1000, timeout_ms=8000, expect=None):
            self.exchanges = getattr(self, "exchanges", 0) + 1
            return J.kline_exchange(self, frame, write_timeout_ms, timeout_ms, expect)
    pending = bytes.fromhex("037F3678") + bytes([(0x03 + 0x7F + 0x36 + 0x78) & 0xFF])
    ch = _X([[pending, bytes.fromhex("00017677")]])
    k = _link_on(ch)
    k.write_bytes(bytes.fromhex("02367FB7"))
    assert ch.writes == [bytes.fromhex("02367FB7")] and ch.exchanges == 2
    assert k.read(4) == bytes.fromhex("00017677")


def _f(hexs):
    """A KWP frame from its header and data, with its checksum."""
    b = bytes.fromhex(hexs)
    return b + bytes([sum(b) & 0xFF])


REQ = _f("02367F")                                   # 02 36 7F B7, address-less
REPLY = _f("000176")                                 # 00 01 76 77 (separate length)
AREQ = _f("8210F12703")                              # addressed
AREPLY = _f("83F1106703AA")


def _timed_read(k, req, n):
    import time as _t
    t0 = _t.monotonic()
    k.write_bytes(req)
    return k.read(n), _t.monotonic() - t0


@pytest.mark.parametrize("stray", [b"\x00", b"\xFF", b"\x55", b"\x00\x55"])
def test_a_stray_byte_before_the_reply_is_skipped(stray):
    ch = _SplitChannel([[stray, REPLY]])
    k = _link_on(ch)
    got, took = _timed_read(k, REQ, 4)
    assert got == REPLY and took < 0.4               # not the whole timeout
    assert not k._rx and not k._carry


@pytest.mark.parametrize("stray", [b"\x00", b"\xFF", b"\x83", b"\x10"])
def test_a_stray_byte_before_an_addressed_reply_is_skipped(stray):
    ch = _SplitChannel([[stray + AREPLY[:3], AREPLY[3:]]])
    k = _link_on(ch)
    got, took = _timed_read(k, AREQ, len(AREPLY))
    assert got == AREPLY and took < 0.4


def test_an_unflagged_echo_of_the_request_is_dropped():
    class _Unflagged(_SplitChannel):
        def write(self, data, flags=0, timeout_ms=100):
            super().write(data, flags, timeout_ms)
            self.queue[0] = J.Msg(J.ISO9141, 0, 0, 0, bytes(data))   # echo, no TX flag
    ch = _Unflagged([[REPLY]])
    k = _link_on(ch)
    got, took = _timed_read(k, REQ, 4)
    assert got == REPLY and took < 0.4 and not k._rx


def test_an_unflagged_echo_and_reply_in_one_buffer():
    class _OneBuffer(_SplitChannel):
        def write(self, data, flags=0, timeout_ms=100):
            self.writes.append(bytes(data))
            self.queue.append(J.Msg(J.ISO9141, 0, 0, 0, bytes(data) + b"".join(self.replies.pop(0))))
    ch = _OneBuffer([[AREPLY]])
    k = _link_on(ch)
    got, _ = _timed_read(k, AREQ, len(AREPLY))
    assert got == AREPLY and not k._rx


def test_two_frames_in_one_buffer_come_out_one_at_a_time():
    second = _f("01C1")
    ch = _SplitChannel([[REPLY + second]])
    k = _link_on(ch)
    k.write_bytes(REQ)
    assert k.read(4) == REPLY and not k._rx          # just the first frame
    assert bytes(k._carry) == second                  # the second kept, not lost
    assert k._recv_message() == second                # and served next, without a read
    k._carry += second
    k.clear_rx()
    assert not k._carry


def test_response_pending_and_the_reply_in_one_buffer():
    pending = _f("037F3678")
    ch = _SplitChannel([[pending + REPLY]])
    k = _link_on(ch)
    k.write_bytes(REQ)
    assert k.read(4) == REPLY and not k._carry


def test_a_reply_longer_than_its_header_is_taken_whole():
    # the EDC16 LVL3 seed: fmt 83 but six bytes of data; and a corrupt reply
    # (bad checksum) is handed on as it came, for the caller to re-read
    seed = _f("83F110670300010002")
    ch = _SplitChannel([[seed]])
    k = _link_on(ch)
    k.write_bytes(AREQ)
    assert k.read(10) == seed
    bad = REPLY[:-1] + b"\x00"
    ch = _SplitChannel([[bad]])
    k = _link_on(ch)
    k.write_bytes(REQ)
    assert k.read(4) == bad


def test_junk_only_ends_at_the_timeout_with_what_came():
    import time as _t
    ch = _SplitChannel([[b"\x55\x55\x55"]])
    k = _link_on(ch)
    k._timeout = 0.2
    t0 = _t.monotonic()
    k.write_bytes(REQ)
    assert k.read(3) == b"\x55\x55\x55" and _t.monotonic() - t0 < 1.0


def test_kline_split_rules():
    S = J.kline_split
    assert S(REPLY) == (0, 4) and S(REPLY, REQ) == (0, 4)           # well-formed: as before
    assert S(REPLY[:3], REQ) is None                                 # in pieces: wait
    assert S(REQ + REPLY, REQ) == (4, 8)                             # unflagged echo
    assert S(REQ[:2], REQ) is None                                   # an echo coming in
    assert S(b"\x00" + REPLY, REQ) == (1, 5)
    assert S(b"\x55" * 40, REQ) is None                              # junk past the window
    assert S(REPLY + REPLY) == (0, 4)
    assert S(b"\x05" + REPLY, REQ) == (1, 5)                        # a stray that looks like a header
    assert S(b"\x3F\x55\x55\x55" + REPLY, REQ) is None              # too far in: wait
    assert S(b"\x3F\x55\x55\x55" + REPLY, REQ, final=True) == (4, 8)  # ...until time is up
    assert S(b"\x05\x05\x05\x05" + REPLY, REQ) == (4, 8)            # runs past a bad frame
    assert S(b"") is None and S(REQ, REQ) is None

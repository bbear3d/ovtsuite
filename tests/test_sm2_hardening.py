# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""SM2 / OpenPort USB hardening: one-packet reads, resync after a timed-out
command, poller threads that pair begin/end_thread and back off, K-line timing
values that must not wrap, and the interface claim that must not leak."""
import threading

import pytest

from ovtsuite import realtime
from ovtsuite.flash import j2534 as J
from ovtsuite.flash import openport as OP
from ovtsuite.flash import sm2can, sm2kline
from ovtsuite.flash import sm2usb as S


class LateFake:
    """The SM2's firmware, with one reply that arrives late: the reply to the
    first ``slow_cmd`` is held back and released ahead of the answer to the NEXT
    command written (it was merely slow, not lost)."""

    def __init__(self, slow_cmd=0x20):
        self.slow_cmd = slow_cmd
        self.slow_done = False
        self.late = b""
        self.out = bytearray()
        self.sent = []

    def write(self, data):
        cmd, n = data[0], data[1] | (data[2] << 8)
        body = bytes(data[4:4 + n])
        self.sent.append(cmd)
        self.out += self.late
        self.late = b""
        if cmd == self.slow_cmd and not self.slow_done:
            self.slow_done = True
            self.late = S.pack(0, b"STALE-" + bytes([cmd]))
            return
        if cmd == S.CMD_PING:
            self.out += S.pack(0, body)
        elif cmd == S.CMD_INFO:
            self.out += S.pack(0, bytes(0x12))
        else:
            self.out += S.pack(0, b"reply-" + bytes([cmd]))

    def read(self, timeout_ms):
        if not self.out:
            return b""
        data, self.out = bytes(self.out[:64]), self.out[64:]
        return data

    def close(self):
        pass


def test_a_late_reply_is_not_taken_for_the_next_commands():
    dev = S.Sm2(LateFake(), handshake=False)
    with pytest.raises(S.Sm2Error):
        dev.command(0x20, b"", 20)                   # reply held back: times out
    pkt = dev.command(0x1C, b"", 200)                # the stale frame arrives first
    assert pkt.payload == b"reply-\x1c"
    assert dev.command(0x1D, b"", 200).payload == b"reply-\x1d"   # and the flag is clear again
    dev.close()


def test_resync_failure_keeps_the_flag_so_the_next_command_tries_again():
    t = LateFake(slow_cmd=0x20)
    dev = S.Sm2(t, handshake=False)
    with pytest.raises(S.Sm2Error):
        dev.command(0x20, b"", 20)
    t.read = lambda ms: b""                          # dead cable: nothing answers the ping
    with pytest.raises(S.Sm2Error):
        dev.command(0x1C, b"", 20)
    assert dev._desync
    del t.read                                       # back to the class's read
    assert dev.command(0x1C, b"", 200).payload == b"reply-\x1c"
    dev.close()


class _Ep:
    wMaxPacketSize = 64

    def __init__(self, packets):
        self.packets = list(packets)
        self.asked = []

    def read(self, size, timeout=0):
        self.asked.append(size)
        if not self.packets:
            raise TimeoutError
        return self.packets.pop(0)


def _sm2_transport(ep):
    import usb.core
    t = S.UsbTransport.__new__(S.UsbTransport)
    t.usb = usb
    t.ep_in = ep
    return t


def test_the_sm2_transport_reads_one_packet_at_a_time(monkeypatch):
    import usb.core
    monkeypatch.setattr(usb.core, "USBTimeoutError", TimeoutError)
    # a 128-byte reply (4 header + 124 payload): two full packets, no short one after them
    frame = S.pack(0, bytes(range(124)))
    assert len(frame) == 128
    ep = _Ep([frame[:64], frame[64:]])
    t = _sm2_transport(ep)
    dev = S.Sm2(t, handshake=False)
    pkt = dev.read_packet(500)
    assert pkt is not None and pkt.payload == bytes(range(124))
    assert ep.asked == [64, 64]                      # never the 512 of OpenPort's read
    assert t.read(5) == b""                          # a timeout is an empty read


def test_openport_read_still_asks_for_a_long_read():
    ep = _Ep([b"x"])
    ep.wMaxPacketSize = 64
    t = OP.UsbTransport.__new__(OP.UsbTransport)
    import usb.core
    t.usb, t.ep_in, t.chunk = __import__("usb"), ep, 64
    t.read(5)
    assert ep.asked == [64 * 8]


# ---- pollers ---------------------------------------------------------------
@pytest.fixture
def rt_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(realtime, "begin_thread", lambda: calls.append("begin"))
    monkeypatch.setattr(realtime, "end_thread", lambda: calls.append("end"))
    return calls


def test_the_can_poller_backs_off_gives_up_and_ends_its_realtime_thread(rt_calls, monkeypatch):
    monkeypatch.setattr(sm2can, "POLL_GIVE_UP", 4)
    waits = []

    class Dev:
        def command(self, *a):
            raise S.Sm2Error("no reply")

    ch = sm2can.Sm2CanChannel.__new__(sm2can.Sm2CanChannel)
    ch.dev, ch._ack, ch.handle, ch._rx, ch._ids = Dev(), False, b"\x80\x02", __import__("collections").deque(), None
    ch._stop = threading.Event()
    real_wait = ch._stop.wait
    ch._stop = type("E", (), {"is_set": lambda s: False, "wait": lambda s, t=None: waits.append(t)})()
    ch._run()
    assert rt_calls == ["begin", "end"]
    assert len(waits) == 3 and all(w >= 0.005 for w in waits)


def test_the_can_poller_ends_its_realtime_thread_on_any_error(rt_calls):
    class Dev:
        def command(self, *a):
            raise RuntimeError("boom")

    ch = sm2can.Sm2CanChannel.__new__(sm2can.Sm2CanChannel)
    ch.dev, ch._ack, ch.handle = Dev(), False, b"\x80\x02"
    ch._stop = threading.Event()
    ch._run()
    assert rt_calls == ["begin", "end"]


def test_the_kline_poller_ends_its_realtime_thread(rt_calls):
    class Dev:
        def command(self, *a):
            raise S.Sm2Error("dead")

    ch = sm2kline.Sm2KLineChannel.__new__(sm2kline.Sm2KLineChannel)
    ch.dev, ch._ack, ch._tester = Dev(), False, 0
    ch._cv, ch._stop, ch._err = threading.Condition(), threading.Event(), None
    ch._run()
    assert rt_calls == ["begin", "end"] and isinstance(ch._err, S.Sm2Error)


# ---- K-line timing values -----------------------------------------------------
def test_kline_timing_values_that_do_not_fit_a_byte_are_refused_not_wrapped():
    assert sm2kline._ms(40) == 20 and sm2kline._ms(510) == 255
    with pytest.raises(S.Sm2Error):
        sm2kline._ms(600, what="P3_MIN")             # 300 ms would have wrapped to 44
    with pytest.raises(S.Sm2Error):
        sm2kline.config_block1(10400, p3_ms=300)
    blk = sm2kline.config_block1(10400, p1_ms=20, p3_ms=55, p4_ms=5)
    assert (blk[sm2kline.OFF_P1], blk[sm2kline.OFF_P3], blk[sm2kline.OFF_P4]) == (20, 55, 5)


def test_set_config_refuses_an_oversize_p3_before_changing_anything():
    ch = sm2kline.Sm2KLineChannel.__new__(sm2kline.Sm2KLineChannel)
    ch.protocol, ch.p1_ms, ch.p3_ms, ch.p4_ms = J.ISO9141, 20, 55, 5
    with pytest.raises(S.Sm2Error):
        ch.set_config(P3_MIN=1000)
    assert ch.p3_ms == 55


# ---- claiming the interface -----------------------------------------------------
class _Dev:
    def __init__(self):
        self.attached = []

    def get_active_configuration(self):
        return object()

    def is_kernel_driver_active(self, n):
        return True

    def detach_kernel_driver(self, n):
        pass

    def attach_kernel_driver(self, n):
        self.attached.append(n)


def test_a_failed_claim_gives_the_kernel_driver_back_and_frees_the_device(monkeypatch):
    import usb.core
    import usb.util
    intf = type("I", (), {"bInterfaceNumber": 1, "bAlternateSetting": 0})()
    ep = type("E", (), {"wMaxPacketSize": 64})()
    monkeypatch.setattr(OP, "find_bulk_interface", lambda cfg, util: (intf, ep, ep))

    def claim(dev, n):
        raise usb.core.USBError("busy")

    disposed = []
    monkeypatch.setattr(usb.util, "claim_interface", claim)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda d: disposed.append(d))
    dev = _Dev()
    with pytest.raises(OP.OpenPortError):
        OP.UsbTransport(dev)
    assert dev.attached == [1] and disposed == [dev]
    with pytest.raises(S.Sm2Error):                  # the SM2 wrapper reports it as its own
        S.UsbTransport(dev)
    assert dev.attached == [1, 1]


# ---- K-line init rest periods ----------------------------------------------------
def test_the_init_rest_periods_are_sliced_and_see_a_stop_request():
    import time
    from ovtsuite.flash import kline_j2534 as K
    from ovtsuite.flash.transport import KLineError
    link = K.J2534KLine.__new__(K.J2534KLine)
    link.is_stop = None
    stop_at = time.monotonic() + 0.15
    link.init_is_stop = lambda: time.monotonic() >= stop_at
    t0 = time.monotonic()
    with pytest.raises(KLineError, match="stop requested"):
        link._quiet(K.INIT_RETRY_QUIET_S)
    assert time.monotonic() - t0 < 1.0               # not the whole 6 s
    link.init_is_stop = None
    t0 = time.monotonic()
    link._quiet(0.12)
    assert 0.1 <= time.monotonic() - t0 < 0.5

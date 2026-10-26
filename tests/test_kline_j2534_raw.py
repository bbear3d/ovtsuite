# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The J2534 byte-stream K-line (EDC15, KW1281): every step is one device
call (``kline_xfer``), a write the caller reads straight after goes with that
read, and a channel without ``kline_xfer`` gets the same reads and writes."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from ovtsuite.flash import diag as D  # noqa: E402
from ovtsuite.flash import j2534 as J  # noqa: E402
from ovtsuite.flash.kline_j2534_raw import J2534RawKLine  # noqa: E402
from test_datalog_edc15 import MockEdc15, _RawChannel, _RawDev  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)


class _XferChannel(_RawChannel):
    """A channel with the one-call step, as the bridge's and the direct
    PassThru's are; counts the steps and any read/write made outside one."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.xfers = []
        self.outside = 0
        self.sent = 0
        self._in = False

    def kline_xfer(self, data, write_timeout_ms=1000, timeout_ms=0, drain=True):
        self.xfers.append((None if data is None else bytes(data), timeout_ms, drain))
        self._in = True
        try:
            return J.kline_xfer(self, data, write_timeout_ms, timeout_ms, drain)
        finally:
            self._in = False

    def read(self, max_msgs=32, timeout_ms=100):
        self.outside += not self._in
        return super().read(max_msgs, timeout_ms)

    def write(self, data, flags=0, timeout_ms=100):
        self.outside += not self._in
        self.sent += len(data)
        return super().write(data, flags, timeout_ms)


class _XferDev(_RawDev):
    def connect(self, protocol, baud, flags=0):
        self.connects.append((protocol, baud, flags))
        self.ecu.baudrate = baud
        self.ch = _XferChannel(self.ecu, self.refuse)
        return self.ch


def test_kw1281_takes_one_device_call_per_byte_handshake():
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    s = D.DiagSession(link, "EDC15", fast_kw1281=True)
    s.connect()
    x0, b0 = len(dev.ch.xfers), dev.ch.sent
    assert [c.text for c in s.read_group(1)] == ["800 rpm", "50.0 mg/stroke", "12.0 V", "90.0 °C"]
    steps, sent = len(dev.ch.xfers) - x0, dev.ch.sent - b0
    # each tester byte (its own, or the complement of the ECU's) goes out in
    # the call that reads what answers it: about one call per byte, where a
    # write and a read were two (and a drain and a collect two more)
    assert sent > 10 and steps <= sent + 2
    assert dev.ch.outside == 0
    # the block end (0x03, not acknowledged) is a write of its own: no wait
    assert (b"\x03", 0, True) in dev.ch.xfers[x0:]
    s.close()
    link.close()


def test_a_write_before_read_is_sent_with_the_read():
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    link.write_before_read(b"\x55")
    assert dev.ch.xfers == []                          # held for the read
    link.read_timeout = 0.05
    with pytest.raises(Exception):
        link.read_byte()                               # the mock does not answer 0x55
    assert dev.ch.xfers[0][0] == b"\x55" and dev.ch.xfers[0][1] > 0


def test_a_held_write_is_sent_before_anything_else():
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    link.write_before_read(b"\x01")
    link.write_byte(0x02)
    assert [d for d, _, _ in dev.ch.xfers] == [b"\x01", b"\x02"]
    link.write_before_read(b"\x03")
    link.clear_rx()
    assert dev.ch.xfers[-1][0] == b"\x03"
    link.write_before_read(b"\x04")
    link.close()
    assert dev.ch.xfers[-1][0] == b"\x04"


def test_write_raw_echo_stays_behind_what_came_before():
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=10400)
    ecu.rx += b"\xAA"                                   # arrived before the write
    link.write_raw(b"\x12\x34")
    assert link.read(3) == b"\xAA\x12\x34"


def test_without_kline_xfer_the_same_session_runs_on_reads_and_writes():
    # e.g. the direct OpenPort channel: J.kline_xfer is run over its
    # read/write, every blocking read asking for one message
    ecu = MockEdc15()
    dev = _RawDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    s = D.DiagSession(link, "EDC15", fast_kw1281=True)
    s.connect()
    assert [c.text for c in s.read_group(1)][0] == "800 rpm"
    s.close()


# ---- EDC15 flash / EEPROM session timing (EDC15_J2534_FAST_TIMING) ---------
from ovtsuite.flash import edc15 as E  # noqa: E402
from ovtsuite.flash import kline_j2534_raw as RAW  # noqa: E402
from test_flash_edc15 import MockEdc15 as FlashEcu  # noqa: E402

_KWP_10_89 = bytes([0x82, 0x01, 0xF1, 0x10, 0x89, 0x0D])   # startDiagnosticSession 89


def _run_edc15(monkeypatch, op, speed="Low"):
    """One EDC15 session on the byte-stream link; returns (device steps,
    sleeps, result)."""
    import time as _t
    sleeps = []
    monkeypatch.setattr(_t, "sleep", sleeps.append)
    ecu = FlashEcu()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    fl = E.Edc15Flash(link)
    loader = {"read": "flash", "full": "flash", "eeprom": "eeprom"}.get(op, "sector")
    fl.connect("V", speed, loader=loader)
    if op == "read":
        out = fl.read_flash() == bytes(ecu.flash)
    elif op == "full":
        img = bytes((i * 5 + 9) & 0xFF for i in range(E.FLASH_SIZE))
        fl.write_flash_full(img)
        out = bytes(ecu.flash) == img
    elif op == "eeprom":
        new = bytearray(ecu.eeprom)
        new[0x10] ^= 0xFF
        new[0x110] ^= 0xFF
        back, n = fl.write_eeprom(bytes(new))
        out = back == bytes(new) and n == 2
    else:
        img = bytearray(ecu.flash)
        img[0x25000] ^= 1
        out = fl.write_flash_sectors(bytes(img)) == [2]
    return dev.ch.xfers, sleeps, out, dev


def _writes(steps):
    """The writes, and whether each waits for a reply in the same call
    (polls of an idle line depend on the clock, so they are left out)."""
    return [(d, w > 0) for d, w, _ in steps if d is not None]


@pytest.mark.parametrize("op, speed", [("read", "Low"), ("read", "High"), ("full", "Low"),
                                       ("sector", "Low"), ("sector", "High"),
                                       ("eeprom", "Low")])
def test_edc15_fast_timing_in_each_phase(monkeypatch, op, speed):
    monkeypatch.setattr(RAW, "EDC15_J2534_FAST_TIMING", True)
    steps, sleeps, ok, dev = _run_edc15(monkeypatch, op, speed)
    assert ok
    # the channel timing is the raw link's either way
    assert dev.ch.config["P1_MAX"] == 2 and dev.ch.config["P4_MIN"] == 0
    # KW1281 identification: R6 1 ms, all of it already waited by the
    # device's P1_MAX before a byte is handed over, so no sleep at all; no
    # 25 ms block lead-in (the flash loader's one 25 ms wait before its
    # RequestDownload is kept)
    assert 0.001 not in sleeps and 0.002 not in sleeps
    assert sleeps.count(0.025) == (1 if op in ("read", "full") else 0)
    # KWP2000: still byte by byte, 5 ms apart (the ECU garbled a whole-frame
    # request on the bench), P3 55 ms kept
    assert not any(d == _KWP_10_89 for d, _, _ in steps)
    assert any(d == bytes([0x82]) for d, _, _ in steps)       # first byte alone
    assert 0.055 in sleeps and 0.005 in sleeps
    # loader commands go out in the call that reads their reply
    # (all but the loader close, 01 A2 A3, read with a timeout of its own)
    big = [(d, w) for d, w in _writes(steps) if len(d) > 3]
    assert (big or op == "eeprom") and all(w for d, w in big)


def test_edc15_handshake_slice_is_one_write_at_low_speed(monkeypatch):
    monkeypatch.setattr(RAW, "EDC15_J2534_FAST_TIMING", True)
    steps, _, ok, _ = _run_edc15(monkeypatch, "read", "Low")
    assert ok
    ardu = E.VARIANTS["V"][1]
    assert any(d == bytes(ardu) + bytes([sum(ardu) & 0xFF]) for d, _, _ in steps)


def test_edc15_timing_switch_off_is_the_previous_timing(monkeypatch):
    monkeypatch.setattr(RAW, "EDC15_J2534_FAST_TIMING", False)
    assert J2534RawKLine.edc15_timing(None) is None
    off = {}
    for op in ("read", "sector", "eeprom"):
        steps, sleeps, ok, _ = _run_edc15(monkeypatch, op, "Low")
        assert ok
        off[op] = (_writes(steps), sleeps)
        # the old values: KW1281 R6 2 ms + 25 ms lead, KWP P3 55 ms and P4 5 ms
        # byte by byte, 5 ms between loader frames
        assert {0.002, 0.025, 0.055, 0.005} <= set(sleeps) and 0.001 not in sleeps
        assert not any(d == _KWP_10_89 for d, _, _ in steps)
        assert any(d == b"\x82" for d, _, _ in steps)          # first byte alone
    # exactly what the flasher did before the switch existed: the same run on
    # a link that offers no EDC15 timing at all
    monkeypatch.delattr(J2534RawKLine, "edc15_timing")
    for op in ("read", "sector", "eeprom"):
        steps, sleeps, ok, _ = _run_edc15(monkeypatch, op, "Low")
        assert ok and (_writes(steps), sleeps) == off[op]


def test_edc15_serial_link_keeps_its_timing(monkeypatch):
    # kwp.py and edc15.py are shared with the serial KKL path: no edc15_timing
    # there, so none of the J2534 values apply whatever the switch says
    import time as _t
    from test_flash_edc15 import _Link
    monkeypatch.setattr(RAW, "EDC15_J2534_FAST_TIMING", True)
    sleeps = []
    monkeypatch.setattr(_t, "sleep", sleeps.append)
    ecu = FlashEcu()
    fl = E.Edc15Flash(_Link(ecu))
    fl.connect("V", "Low", loader="eeprom")
    assert fl._timing == {} and not fl.kwp.fast
    assert {0.002, 0.025, 0.055, 0.005} <= set(sleeps)


# ---- logging speed and Cancel (2026-10-25) ---------------------------------
class _Msg:
    def __init__(self, payload):
        self.payload = payload
        self.is_tx_echo = False


class _IndicatingChannel:
    """A device that reports the start of a message first (an empty
    indication, as the Scanmatik does) and the byte itself one read later."""

    def __init__(self, byte=b"\x5A"):
        self.queue = [[_Msg(b"")], [], [_Msg(byte)]]
        self.reads = []

    def read(self, max_msgs=32, timeout_ms=0):
        self.reads.append((max_msgs, timeout_ms))
        return self.queue.pop(0) if self.queue else []

    def write(self, data, flags=0, timeout_ms=0):
        pass


def test_kline_xfer_waits_past_an_empty_start_indication():
    ch = _IndicatingChannel()
    before, after = J.kline_xfer(ch, b"\x12", 1000, 50, drain=False)
    # the byte behind the indication comes back in the same call, where it
    # used to cost the link a second round trip for every KW1281 byte
    assert [m.payload for m in after] == [b"\x5A"]
    assert before == []


def test_kline_xfer_gives_up_at_its_timeout_on_indications_alone():
    ch = _IndicatingChannel()
    ch.queue = [[_Msg(b"")]] * 200
    _, after = J.kline_xfer(ch, None, 1000, 1, drain=False)
    assert after == [] and len(ch.reads) < 200


def test_kw1281_fast_r6_is_not_waited_twice_on_the_raw_link(monkeypatch):
    from ovtsuite.flash import kwp as K
    assert J2534RawKLine.rx_settle_s == 0.001
    link = type("L", (), {"rx_settle_s": 0.001})()
    assert K.Kw1281(link, fast=True)._r6 == 0.0
    assert K.Kw1281(link, fast=False)._r6 == K.R6        # the flasher's old timing
    assert K.Kw1281(object(), fast=True)._r6 == K.R6_FAST  # a serial cable


def test_raw_wake_up_gives_up_on_cancel_between_tries(monkeypatch):
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    tries = []

    def fail(addr):
        tries.append(addr)
        stop["on"] = True                              # Cancel pressed meanwhile
        raise J.J2534Error("timeout")
    stop = {"on": False}
    monkeypatch.setattr(link, "_wake_up_once", fail)
    link.is_stop = lambda: stop["on"]
    from ovtsuite.flash.transport import LinkStopped
    with pytest.raises(LinkStopped):
        link.wake_up(0x01)
    assert len(tries) == 1


def test_the_flashers_cancel_stops_init_retries_but_not_reads(monkeypatch):
    ecu = MockEdc15()
    dev = _XferDev(ecu)
    link = J2534RawKLine(dev, baud=9600)
    link.init_is_stop = lambda: True                   # Cancel pressed
    ecu.rx += b"\x42"
    assert link.read_byte() == 0x42                    # a read is not cut
    monkeypatch.setattr(link, "_wake_up_once",
                        lambda a: (_ for _ in ()).throw(J.J2534Error("x")))
    from ovtsuite.flash.transport import LinkStopped
    with pytest.raises(LinkStopped):
        link.wake_up(0x01)


def test_serial_kline_init_retries_see_the_flashers_cancel(monkeypatch):
    from ovtsuite.flash import transport as T
    link = T.KLine.__new__(T.KLine)
    link.prefer_fast_init = False
    link.init_is_stop = lambda: True
    with pytest.raises(T.LinkStopped):
        link.wake_up(0x01)

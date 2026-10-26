# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""edc16_program.read_full pads with 0xFF only what the ECU explicitly refuses
(7F 35 / 7F 36). An ECU that stops answering, or an upload that ends early,
fails the read -- a full read must never hand back 0xFF for data it did not
get (the image may be saved, compared or written back)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.flash import edc16 as E  # noqa: E402
from ovtsuite.flash import edc16_program as P  # noqa: E402
from ovtsuite.flash import tp20_transport as TT  # noqa: E402
from ovtsuite.flash.transport import KLineError, KLineTimeout  # noqa: E402
from mock_tp20 import MockTp20Ecu  # noqa: E402

BASE = 0x040000
SIZE = 0x10000                       # two 32 KB uploads (four 16 KB halves)


def _byte(addr):
    return (addr * 7 + 3) & 0xFF


class Ecu(MockTp20Ecu):
    """Read-session EDC16. ``dead_after`` packets: then it answers nothing at
    all (no ACK, no reply). ``refuse``: an address whose 16 KB is refused with
    7F 35 33. ``short_at``: an upload start that ends after one packet."""

    def __init__(self, dead_after=None, refuse=None, short_at=None):
        super().__init__(self._handle)
        self.dead_after = dead_after
        self.refuse = refuse
        self.short_at = short_at
        self.short_times = -1               # how many uploads end short (-1: all)
        self.packets = 0
        self.dead = False
        self.pos = self.end = 0
        self.start = None
        self.cur_short = False

    def write_can(self, can_id, payload, extended=False, timeout_ms=100):
        if self.dead:
            return                                   # the channel is gone
        super().write_can(can_id, payload, extended, timeout_ms)

    def _handle(self, req):
        s = req[0]
        if s == 0x27 and req[1] == 0x03:
            return bytes([0x67, 0x03, 0x11, 0x22, 0x33, 0x44])
        if s == 0x27 and req[1] == 0x04:
            return bytes([0x67, 0x04, 0x34])
        if s == 0x10:
            return bytes([0x50, req[1]])
        if s == 0x35:
            addr = int.from_bytes(req[1:4], "big")
            self.cur_short = addr == self.short_at and self.short_times != 0
            if self.cur_short and self.short_times > 0:
                self.short_times -= 1
            size = int.from_bytes(req[5:8], "big")
            if self.refuse is not None and addr <= self.refuse < addr + size:
                return bytes([0x7F, 0x35, 0x33])
            self.start, self.pos, self.end = addr, addr, addr + size
            return b"\x75\xff"
        if s == 0x36:
            self.packets += 1
            if self.dead_after is not None and self.packets > self.dead_after:
                self.dead = True
                return None
            n = 254
            if self.cur_short:
                self.end = min(self.end, self.pos + 254 + 10)    # last packet short
            chunk = bytes(_byte(a) for a in range(self.pos, min(self.pos + n, self.end)))
            self.pos += len(chunk)
            return b"\x76" + chunk
        if s == 0x37:
            return b"\x77"
        if s == 0x82:
            return b"\xc2"
        return b"\x7f" + bytes([s, 0x11])


def _read(monkeypatch, ecu):
    monkeypatch.setattr(P, "PROGRAM_START", BASE)
    monkeypatch.setattr(P, "PROGRAM_END", BASE + SIZE)
    monkeypatch.setattr(E, "EDC16_CAN_READ_REOPEN", False)
    link = TT.CanKLine(ecu, dest=0x01)
    stages = []
    fl = E.Edc16Flash(link, log=stages.append)
    fl._fast = True
    out = P.read_full(fl, include_program=True, include_mpc=False, full=False,
                      upload_chunk=0x8000, allow_fast=False, on_stage=stages.append)
    return out, stages


def test_full_read_returns_the_data(monkeypatch):
    out, _stages = _read(monkeypatch, Ecu())
    assert out["program"] == bytes(_byte(a) for a in range(BASE, BASE + SIZE))


def test_refused_range_is_padded_and_logged(monkeypatch):
    refused = BASE + 0xC000
    out, stages = _read(monkeypatch, Ecu(refuse=refused))
    want = bytearray(_byte(a) for a in range(BASE, BASE + SIZE))
    want[0xC000:0x10000] = b"\xFF" * 0x4000
    assert out["program"] == bytes(want)
    assert any("0x04C000-0x04FFFF padded with 0xFF -- the ECU refused it" in m
               and "7F 35 33" in m for m in stages)


@pytest.mark.slow
def test_ecu_that_stops_answering_fails_the_read(monkeypatch):
    """Dead mid-read: an error naming the range, not a 0xFF-padded image."""
    ecu = Ecu(dead_after=100)                        # ~25 KB into the first upload
    with pytest.raises(KLineTimeout) as ei:
        _read(monkeypatch, ecu)
    assert "no answer from the ECU" in str(ei.value)
    assert "incomplete" in str(ei.value)


def test_upload_that_ends_early_fails_the_read(monkeypatch):
    """The ECU ends an upload short every time: retried smaller, then the read
    fails -- the missing bytes are never padded."""
    ecu = Ecu(short_at=BASE + 0x8000)
    # every upload starting there ends short, including the 16 KB retry
    with pytest.raises(KLineError) as ei:
        _read(monkeypatch, ecu)
    assert "0x048000-0x04BFFF could not be read" in str(ei.value)
    assert "ended after" in str(ei.value)



# ---------------------------------------------- Edc16Flash.read_flash (calibration)
def _read_cal(monkeypatch, ecu):
    monkeypatch.setattr(E, "EDC16_CAN_READ_REOPEN", False)
    link = TT.CanKLine(ecu, dest=0x01)
    logs = []
    fl = E.Edc16Flash(link, log=logs.append)
    fl._fast = True
    img = fl.read_flash(speed="Medium", allow_fast=False, upload_chunk=0x10000,
                        on_stage=logs.append)
    return img, logs


@pytest.mark.slow
def test_cal_read_rereads_a_short_upload(monkeypatch):
    """One upload ends early: the missing tail is read again, not 0xFF."""
    ecu = Ecu(short_at=E.CAL_START + 0x20000)
    ecu.short_times = 1
    img, logs = _read_cal(monkeypatch, ecu)
    assert img[E.CAL_START:] == bytes(_byte(a) for a in range(E.CAL_START, E.FULL_SIZE))
    assert any("ended 0x" in m and "short" in m for m in logs)
    assert any("Re-reading 0x1A0000-0x1AFFFF" in m for m in logs)


@pytest.mark.slow
def test_cal_read_fails_when_an_upload_stays_short(monkeypatch):
    ecu = Ecu(short_at=E.CAL_START + 0x20000)          # short every time
    with pytest.raises(KLineError) as ei:
        _read_cal(monkeypatch, ecu)
    assert "0x1A0000-0x1AFFFF stayed corrupt or missing" in str(ei.value)

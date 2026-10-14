# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Live-data / fault-code session against mock ECUs, plus the log formats."""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite.flash import diag as D  # noqa: E402
from ovtsuite.flash.transport import KLineTimeout, adjust_parity  # noqa: E402
from ovtsuite.datalog import logfile as L  # noqa: E402


def _ck(data):
    return sum(data) & 0xFF


# ------------------------------------------------------------- mock ECUs
class _Serial:
    """pyserial-ish buffer shared by both mocks."""

    def __init__(self):
        self.rx = bytearray()
        self.parse = bytearray()
        self.baudrate = 10400
        self.timeout = 8.0
        self.break_condition = False
        self._broke = False
        self.log = []

    def flush(self):
        pass

    def close(self):
        pass

    def __setattr__(self, k, v):
        if k == "break_condition" and v:
            object.__setattr__(self, "_broke", True)
        object.__setattr__(self, k, v)

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.rx += data
        self.parse += data
        self._consume()


class MockEdc16(_Serial):
    """KWP2000: 10 89 (+speed), 21 <group>, 18, 14, 3E, 1A 9B, 82."""

    GROUPS = {
        1: [(1, 200, 20), (49, 40, 50), (15, 10, 80), (5, 10, 190)],
        11: [(1, 200, 20), (18, 25, 250), (18, 25, 245), (23, 100, 128)],
    }

    def __init__(self, refuse_speed=False, drop_after=None, dtcs=None):
        super().__init__()
        self.refuse_speed = refuse_speed
        self.drop_after = drop_after          # stop answering after N group reads
        self.reads = 0
        self.session = False
        self.dtcs = list(dtcs if dtcs is not None else [(19558, 0x40), (16485, 0xE2)])
        self.mode = "idle"
        self.cleared = 0
        self.speed_requests = []

    def reset_input_buffer(self):
        self.rx.clear()
        if self._broke:
            self._broke = False
            self.parse.clear()
            if self.baudrate == 10400:
                self.rx += bytes([0x55, 0xEF, 0x8F])          # KW 2031
                self.mode = "kw"
            self.session = False

    def _consume(self):
        p = self.parse
        while p:
            if self.mode == "kw":
                if p[0] == (~0x8F) & 0xFF:
                    del p[0]
                    self.rx += bytes([(~adjust_parity(0x01, False)) & 0xFF])
                    self.mode = "kwp"
                    continue
                return
            if self.mode == "kwp":
                if not self._frame():
                    return
                continue
            return

    def _reply(self, svc, body):
        n = len(body) + 1
        frame = bytes([0x80 | n, 0xF1, 0x10, svc]) + bytes(body)
        self.rx += frame + bytes([_ck(frame)])

    def _frame(self):
        p = self.parse
        fmt = p[0]
        hdr = 3 if fmt & 0x80 else 1
        n = fmt & 63
        total = hdr + n + 1
        if len(p) < total:
            return False
        frame = bytes(p[:total])
        del p[:total]
        assert frame[-1] == _ck(frame[:-1]), "KWP checksum"
        svc, body = frame[hdr], frame[hdr + 1:-1]
        self.log.append((svc, bytes(body)))
        if self.drop_after is not None and self.reads >= self.drop_after:
            return True                                    # silence
        if svc == 0x10:
            if len(body) == 2:
                self.speed_requests.append(body[1])
                if self.refuse_speed:
                    self._reply(0x7F, bytes([0x10, 0x12]))
                    return True
                self.baudrate = {0x50: 38400, 0x87: 124800, 0xA7: 244898}[body[1]]
            self.session = True
            self._reply(0x50, bytes(body))
        elif svc == 0x1A:
            rec = b"\x9B" + b"038906016AB " + b"4999" + b"\x03" + b"\x00\x01\x02" + b"\x00" * 6 + b"1.9l R4 EDC G000SG "
            self._reply(0x5A, rec)
        elif svc == 0x21:
            if not self.session:
                self._reply(0x7F, bytes([0x21, 0x12]))
                return True
            g = body[0]
            self.reads += 1
            cells = self.GROUPS.get(g)
            if cells is None:
                self._reply(0x7F, bytes([0x21, 0x31]))
                return True
            out = bytearray([g])
            for f, a, b in cells:
                out += bytes([f, a, b])
            while len(out) < 1 + 8 * 3:
                out += bytes([0x25, 0, 0])
            self._reply(0x61, out)
        elif svc == 0x18:
            out = bytearray([len(self.dtcs)])
            for code, st in self.dtcs:
                out += bytes([code >> 8, code & 0xFF, st])
            self._reply(0x58, out)
        elif svc == 0x14:
            self.dtcs = []
            self.cleared += 1
            self._reply(0x54, bytes(body))
        elif svc == 0x3E:
            self._reply(0x7E, b"")
        elif svc == 0x82:
            self._reply(0xC2, b"")
        else:
            self._reply(0x7F, bytes([svc, 0x11]))
        return True


class MockEdc15(_Serial):
    """KW1281 at 9600: ident blocks, 29 <group> -> E7, 07 -> FC, 05, ACK."""

    def __init__(self):
        super().__init__()
        self.baudrate = 9600
        self.mode = "idle"
        self.counter = 0
        self._out_block = None
        self._in_block = None
        self.pending = []
        self.dtcs = [(17664, 0x23)]
        self.cleared = 0

    def reset_input_buffer(self):
        self.rx.clear()
        if self._broke:
            self._broke = False
            self.parse.clear()
            if self.baudrate == 9600:
                self.rx += bytes([0x55, 0x01, 0x8A])
                self.mode = "kw"
            else:
                self.rx += bytes([0x95])
                self.mode = "idle"

    def _consume(self):
        p = self.parse
        while p:
            if self.mode == "kw":
                if p[0] == (~0x8A) & 0xFF:
                    del p[0]
                    self.mode = "kw1281"
                    self.counter = 1
                    self._send_block(0xF6, b"038906012A   ")
                    self.pending = [(0xF6, b"1.9l R4 EDC  ")]
                    continue
                return
            if self.mode == "kw1281":
                if not self._byte(p[0]):
                    return
                del p[0]
                continue
            return

    def _send_block(self, title, data):
        blk = bytes([len(data) + 3, self.counter, title]) + bytes(data)
        self.counter = (self.counter + 1) & 0xFF
        self._out_block = list(blk)
        self._out_idx = 0
        self.rx.append(self._out_block[0])
        self._in_block = None

    def _byte(self, b):
        if self._out_block is not None:
            want = (~self._out_block[self._out_idx]) & 0xFF
            assert b == want, "KW1281 complement mismatch"
            self._out_idx += 1
            if self._out_idx < len(self._out_block):
                self.rx.append(self._out_block[self._out_idx])
            else:
                self.rx.append(0x03)
                self._out_block = None
                self._in_block = bytearray()
            return True
        if self._in_block is None:
            self._in_block = bytearray()
        if b == 0x03 and len(self._in_block) >= 3 and len(self._in_block) == self._in_block[0]:
            blk = bytes(self._in_block)
            self._in_block = None
            assert blk[1] == self.counter, "tester block counter"
            self.counter = (self.counter + 1) & 0xFF
            title = blk[2]
            self.log.append((title, bytes(blk[3:])))
            if title == 0x09:
                if self.pending:
                    t, d = self.pending.pop(0)
                    self._send_block(t, d)
                else:
                    self._send_block(0x09, b"")
            elif title == 0x29:
                g = blk[3]
                if g == 1:
                    self._send_block(0xE7, bytes([1, 200, 20, 49, 40, 50, 6, 100, 120, 5, 10, 190]))
                else:
                    self._send_block(0x0A, b"")
            elif title == 0x07:
                data = bytearray()
                for code, st in self.dtcs:
                    data += bytes([code >> 8, code & 0xFF, st])
                if not data:
                    data = bytes([0xFF, 0xFF, 0x88])
                self._send_block(0xFC, data)
                self.pending = [(0x09, b"")]
            elif title == 0x05:
                self.dtcs = []
                self.cleared += 1
                self._send_block(0xFC, bytes([0xFF, 0xFF, 0x88]))
                self.pending = [(0x09, b"")]
            elif title == 0x06:
                self.mode = "idle"
            return True
        self._in_block.append(b)
        self.rx.append((~b) & 0xFF)
        return True


class _Link:
    def __init__(self, ecu):
        self.port = ecu
        self._t = 8.0
        self.last_sync = -1

    @property
    def read_timeout(self):
        return self._t

    @read_timeout.setter
    def read_timeout(self, v):
        self._t = v

    def set_baud(self, b):
        self.port.baudrate = b

    def set_break(self, on):
        self.port.break_condition = on

    def clear_rx(self):
        self.port.reset_input_buffer()

    def read_byte(self):
        b = self.port.read(1)
        if not b:
            raise KLineTimeout("timeout")
        return b[0]

    def read(self, n):
        out = bytearray()
        while len(out) < n:
            c = self.port.read(n - len(out))
            if not c:
                raise KLineTimeout("timeout")
            out += c
        return bytes(out)

    def write_raw(self, data):
        self.port.write(data)

    def write_byte(self, b):
        self.port.write(bytes([b]))
        self.port.read(1)

    def write_bytes(self, data):
        self.port.write(data)
        self.port.read(len(data))

    def read_complement(self, b):
        got = self.read_byte()
        assert got == (~b) & 0xFF, "complement"

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        self.set_break(False)
        self.set_break(True)
        self.clear_rx()
        sync = self.read_byte()
        self.last_sync = sync
        if sync != 0x55:
            from ovtsuite.flash.transport import KLineError
            raise KLineError("Unexpected sync byte 0x%02X" % sync)
        lsb = self.read_byte()
        msb = self.read_byte()
        version = ((msb & 0x7F) << 7) + (lsb & 0x7F)
        self.write_byte((~msb) & 0xFF)
        if version >= 2000:
            self.read_complement(adjust_parity(address, even_parity))
        return version


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)


# ---------------------------------------------------------------- formulas
def test_formula_table():
    assert D.decode_cell(1, 200, 20) == (800.0, "rpm", "800 rpm")
    v, u, t = D.decode_cell(5, 10, 190)
    assert (round(v, 1), u) == (90.0, "°C")
    assert D.decode_cell(18, 25, 250)[0] == pytest.approx(250.0)
    assert D.decode_cell(16, 0xA5, 0x01)[2] == "10100101 00000001"
    assert D.decode_cell(99, 1, 2)[0] is None
    cells = D.parse_cells(bytes([11, 1, 200, 20, 0x25, 0, 0, 0x25, 0, 0]))
    assert len(cells) == 1 and cells[0].value == 800.0


# --------------------------------------------------------------- EDC16 path
def test_edc16_session_medium_speed_and_groups():
    ecu = MockEdc16()
    s = D.DiagSession(_Link(ecu), "EDC16")
    assert s.connect("Medium") == 124800
    assert ecu.speed_requests == [0x87] and ecu.baudrate == 124800
    assert s.identity.startswith("038906016AB") and "SW 4999" in s.identity
    g1 = s.read_group(1)
    assert [c.text for c in g1][0] == "800 rpm" and len(g1) == 4
    assert s.read_group(200) is None                       # NAK -> not available
    res = s.read_groups([1, 11])
    assert res[11][1].unit == "mbar" and res[11][1].value == pytest.approx(250.0)
    s.keepalive()
    assert (0x3E, b"") in ecu.log
    s.close()
    assert ecu.log[-1][0] == 0x82


def test_edc16_speed_refused_stays_at_10400_and_ch34x_cap():
    ecu = MockEdc16(refuse_speed=True)
    s = D.DiagSession(_Link(ecu), "EDC16")
    assert s.connect("High") == 10400
    assert ecu.baudrate == 10400 and s.read_group(1) is not None
    ecu2 = MockEdc16()
    s2 = D.DiagSession(_Link(ecu2), "EDC16")
    s2.connect("High", cap_high=True)
    assert ecu2.speed_requests == [0x87] and s2.baud == 124800
    ecu3 = MockEdc16()
    s3 = D.DiagSession(_Link(ecu3), "EDC16")
    assert s3.connect("Low") == 10400 and ecu3.speed_requests == []


def test_edc16_dtcs_read_and_clear():
    ecu = MockEdc16()
    s = D.DiagSession(_Link(ecu), "EDC16")
    s.connect("Low")
    codes = s.read_dtcs()
    assert [(c.code, c.status) for c in codes] == [(19558, 0x40), (16485, 0xE2)]
    assert codes[0].text.startswith("19558")
    s.clear_dtcs()
    assert ecu.cleared == 1 and s.read_dtcs() == []


def test_edc16_reconnect_after_dropout():
    ecu = MockEdc16(drop_after=3)          # the speed check after 10 89 is read #1
    s = D.DiagSession(_Link(ecu), "EDC16")
    s.connect("Medium")
    assert s.read_group(1) is not None and s.read_group(1) is not None
    with pytest.raises(KLineTimeout):
        s.read_group(1)
    ecu.drop_after = None
    s.reconnect()
    assert s.baud == 124800 and s.read_group(1) is not None


# --------------------------------------------------------------- EDC15 path
def test_edc15_kw1281_groups_and_faults():
    ecu = MockEdc15()
    s = D.DiagSession(_Link(ecu), "EDC15")
    assert s.connect() == 9600
    assert "038906012A" in s.identity
    g = s.read_group(1)
    assert [c.text for c in g] == ["800 rpm", "50.0 mg/stroke", "12.0 V", "90.0 °C"]
    assert s.read_group(5) is None
    codes = s.read_dtcs()
    assert [(c.code, c.status) for c in codes] == [(17664, 0x23)]
    s.clear_dtcs()
    assert ecu.cleared == 1 and s.read_dtcs() == []
    s.keepalive()
    s.close()
    assert ecu.log[-1][0] == 0x06


# --------------------------------------------------------------- log files
def _sample():
    chs = [L.Channel(1, 0, "Engine Speed (Eng_nAvrg)", "rpm", "Eng_nAvrg"),
           L.Channel(11, 1, "Boost Pressure Setpoint (PCR_pBDes)", "mbar", "PCR_pBDes")]
    log = L.DataLog(channels=chs, identity="038906016AB  SW 4999")
    t0 = datetime(2026, 9, 6, 14, 3, 11, 250000)
    for i in range(4):
        log.append(t0 + timedelta(milliseconds=110 * i), ["%d rpm" % (800 + 100 * i), "%.1f mbar" % (1000 + 10 * i)])
    return log


def test_kwhack_layout_roundtrip_and_marker():
    log = _sample()
    d = tempfile.mkdtemp()
    p = os.path.join(d, "k.csv")
    L.write_kwhack(p, log)
    txt = open(p).read().splitlines()
    assert txt[0] == "# Cells: 1.1,11.2"
    assert txt[1].startswith("Timestamp,Engine Speed")
    back = L.load(p)
    assert back.layout == "kwhack"
    assert [c.key for c in back.channels] == [(1, 0), (11, 1)]
    assert back.values[3] == [1100.0, 1030.0]
    assert back.channels[1].unit == "mbar"
    assert back.seconds() == pytest.approx([0, 0.11, 0.22, 0.33])
    assert L.parse_marker_line("# Cells: 3.4,1.1") == [L.Channel(3, 3), L.Channel(1, 0)]
    assert L.parse_marker_line("# Cells: x") is None


def test_vcds_layout_roundtrip():
    log = _sample()
    d = tempfile.mkdtemp()
    p = os.path.join(d, "v.csv")
    L.write_vcds(p, log)
    lines = open(p).read().splitlines()
    assert lines[1].startswith("038906016AB,ADVMB,")
    assert lines[2] == ",,G001,F0,G011,F1,"
    assert lines[4].startswith("Marker,TIME,Group 1 - Field 0,")
    back = L.load(p)
    assert back.layout == "vcds"
    assert [c.key for c in back.channels] == [(1, 0), (11, 1)]
    assert [c.unit for c in back.channels] == ["rpm", "mbar"]
    assert back.values[2] == [1000.0, 1020.0]
    assert back.identity.startswith("038906016AB")
    assert back.seconds() == pytest.approx([0, 0.11, 0.22, 0.33])


def test_channel_namer_without_binary():
    from ovtsuite.datalog.channels import ChannelNamer
    n = ChannelNamer(None)
    assert not n.available
    assert n.channel(7, 2).title == "Group 7 / Cell 3"


def test_quantity_per_stroke_is_not_per_hour():
    """VAG's "mg/H" (formulas 39, 49, 51) is per Hub -- per stroke. The table
    once said "mg/h", which read as milligrams per hour; logs written then
    read back per stroke."""
    from ovtsuite.flash.diag import decode_cell
    from ovtsuite.datalog.logfile import split_value_unit
    for f in (39, 49, 51):
        assert decode_cell(f, 10, 140)[1] == "mg/stroke", f
    assert decode_cell(35, 10, 140)[1] == "l/h"          # really per hour
    assert split_value_unit("3.4 mg/h") == (3.4, "mg/stroke")
    assert split_value_unit("2.1 l/h") == (2.1, "l/h")


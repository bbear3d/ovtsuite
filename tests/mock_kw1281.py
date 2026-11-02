# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A scripted KW1281 control module behind a pyserial-ish port, plus the
K-line facade the diag session drives, for exercising the generic KW1281
command set (ovtsuite/diag/kw1281.py) without a car.

Modelled on the MockEdc15 in test_datalog.py (5-baud init at 9600, ident
blocks, 29 -> E7, 07 -> FC, 05) and extended with the whole block set
KWHack's KW1281Dialog uses: 21/22/2A adaptation, 2B login, 10 coding,
04 actuator test, 28 basic setting, 19/03/01 memory reads, 1A EEPROM write.
"""
from ovtsuite.flash.transport import KLineTimeout, adjust_parity


class _Serial:
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


class MockKw1281Module(_Serial):
    """A KW1281 module at ``address`` that answers only at ``baud``."""

    def __init__(self, address=0x17, baud=10400, part="4F0920930N", desc="KOMBIINSTR. VDO",
                 coding=7231, wsc=12345, login_code=12345):
        super().__init__()
        self.address = address
        self.baud = baud
        self.part, self.desc = part, desc
        self.coding, self.wsc = coding, wsc
        self.login_code = login_code
        self.logged_in = False
        self.mode = "idle"
        self.counter = 0
        self._out_block = None
        self._in_block = None
        self.pending = []
        self.dtcs = [(1314, 0x23)]
        self.adaptation = {2: 150, 9: 0, 40: 300}       # channel -> value
        self.saved = []                                  # (channel, value, wsc)
        self.tested = []
        self.eeprom = bytes(range(256)) * 8
        self.actuators = [0x0296, 0x0295, 0x04AB]        # ends with 04 AB
        self._act_idx = 0
        self.groups = {1: bytes([1, 200, 20, 49, 40, 50, 6, 100, 120, 5, 10, 190])}
        self.basic = {60: bytes([1, 200, 30, 49, 40, 60, 6, 100, 120, 5, 10, 190])}
        self.wrote = []

    # ------------------------------------------------------------ wake-up
    def reset_input_buffer(self):
        self.rx.clear()
        if self._broke:
            self._broke = False
            self.parse.clear()
            if self.baudrate == self.baud:
                self.rx += bytes([0x55, 0x01, 0x8A])
                self.mode = "kw"
            else:
                self.rx += bytes([0x95])
                self.mode = "idle"

    def _ident_blocks(self):
        text = (self.part + " " + self.desc).encode()
        pieces = [text[i:i + 12].ljust(12) for i in range(0, len(text), 12)]
        blocks = [(0xF6, p) for p in pieces]
        c2 = self.coding * 2
        lo = c2 & 0xFF
        if self.wsc > 65535:
            lo |= 1
        blocks.append((0xF6, bytes([0x00, (c2 >> 8) & 0xFF, lo, (self.wsc & 0xFFFF) >> 8, self.wsc & 0xFF])))
        return blocks

    def _consume(self):
        p = self.parse
        while p:
            if self.mode == "kw":
                if p[0] == (~0x8A) & 0xFF:
                    del p[0]
                    self.mode = "kw1281"
                    self.counter = 1
                    first, rest = self._ident_blocks()[0], self._ident_blocks()[1:]
                    self._send_block(*first)
                    self.pending = rest
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

    def _nak(self):
        self._send_block(0x0A, b"")

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
            self._handle(blk[2], bytes(blk[3:]))
            return True
        self._in_block.append(b)
        self.rx.append((~b) & 0xFF)
        return True

    # ------------------------------------------------------------ blocks
    def _handle(self, title, body):
        self.log.append((title, body))
        if title == 0x09:                                # ACK: next pending, else ACK back
            if self.pending:
                t, d = self.pending.pop(0)
                self._send_block(t, d)
            else:
                self._send_block(0x09, b"")
        elif title == 0x29 or title == 0x28:
            table = self.groups if title == 0x29 else self.basic
            data = table.get(body[0])
            self._send_block(0xE7, data) if data is not None else self._nak()
        elif title == 0x12 or title == 0x11:
            self._send_block(0xF4, bytes([0x10, 0x20, 0x30, 0x40]))
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
            self._send_block(0xFC, bytes([0xFF, 0xFF, 0x88]))
            self.pending = [(0x09, b"")]
        elif title == 0x2B:
            code = (body[0] << 8) | body[1]
            self.logged_in = code == self.login_code
            self._send_block(0x09, b"") if self.logged_in else self._nak()
        elif title == 0x21:
            ch = body[0]
            if ch not in self.adaptation:
                return self._nak()
            v = self.adaptation[ch]
            self._send_block(0xE6, bytes([ch, v >> 8, v & 0xFF, 0x01, 0x01, 0x02]))
        elif title == 0x22:
            ch, v = body[0], (body[1] << 8) | body[2]
            self.tested.append((ch, v))
            self._send_block(0xE6, bytes([ch, v >> 8, v & 0xFF]))
        elif title == 0x2A:
            if not self.logged_in:
                return self._nak()
            ch, v = body[0], (body[1] << 8) | body[2]
            w = (body[3] << 16) | (body[4] << 8) | body[5]
            self.adaptation[ch] = v
            self.saved.append((ch, v, w))
            self._send_block(0xE6, bytes([ch, v >> 8, v & 0xFF]))
        elif title == 0x10:
            c2 = (body[0] << 8) | body[1]
            self.coding = c2 // 2
            self.wsc = ((body[2] << 8) | body[3]) + (65536 if body[1] & 1 else 0)
            first, rest = self._ident_blocks()[0], self._ident_blocks()[1:]
            self._send_block(*first)
            self.pending = rest + [(0x09, b"")]
        elif title == 0x04:
            aid = self.actuators[min(self._act_idx, len(self.actuators) - 1)]
            self._act_idx += 1
            self._send_block(0xF5, bytes([aid >> 8, aid & 0xFF]))
        elif title in (0x19, 0x03, 0x01):
            count, addr = body[0], (body[1] << 8) | body[2]
            if title == 0x19 and not self.logged_in:
                return self._nak()
            resp = {0x19: 0xEF, 0x03: 0xFD, 0x01: 0xFE}[title]
            self._send_block(resp, self.eeprom[addr:addr + count])
        elif title == 0x1A:
            count, addr = body[0], (body[1] << 8) | body[2]
            self.wrote.append((addr, bytes(body[3:3 + count])))
            self._send_block(0xF9, bytes(body[:3]))
            self.pending = [(0x09, b"")]
        elif title == 0x06:
            self.mode = "idle"
        else:
            self._nak()


class Link:
    """The K-line facade over the mock port (what KLine presents)."""

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

    def close(self):
        pass

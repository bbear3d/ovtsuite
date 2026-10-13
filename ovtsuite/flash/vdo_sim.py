# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A byte-level KW1281 VDO cluster, for exercising the cluster flasher without
a car.

:class:`SimClusterLink` stands in for :class:`ovtsuite.flash.transport.KLine`
(``set_baud`` / ``wake_up`` / ``read_byte`` / ``write_byte`` /
``read_complement`` / ``read`` / ``clear_rx`` / ``close``) and behaves like
the cluster on the wire: every byte of a block is acknowledged with its
complement except the 0x03 terminator, the block counter advances on every
block in either direction, a data block from the cluster is answered with an
ACK block from the tester, and a dialogue ends with the cluster's ACK.

The cluster logic follows what KWHack's VdoCluster path expects: custom
command unlock (``1B 80 01 02 03 04``), ReadSoftwareVersion (``1B 84 nn``),
the per-ROM unlock code (``1B 9D``), access level / seed / key (``1B 96``),
ReadEeprom (``19``) and WriteEeprom (``1A``) -- EEPROM access is NAKed until
the unlock code AND the seed/key login have both been accepted, like a real
cluster at access level < 7.
"""
from __future__ import annotations

import os
from typing import List, Optional

from .transport import KLineError, KLineTimeout
from . import vdo_cluster as _vc


class SimClusterLink:
    def __init__(self, eeprom: bytes, version: str = "VWK501MH 01.10",
                 ident: str = "1J0920806M  KOMBI+WEGFAHRSP VDO V07",
                 require_seed_key: bool = True, refuse_write_at=None,
                 corrupt_write_at=None):
        from ..platforms.vdo import rom_bytes
        self.eeprom = bytearray(eeprom)
        self.version = version
        self.version_bytes = rom_bytes(version)
        self.ident = ident
        self.unlock_code = _vc.unlock_codes(version)[0]
        self.require_seed_key = require_seed_key
        self.refuse_write_at = set(refuse_write_at or ())
        self.corrupt_write_at = set(corrupt_write_at or ())
        self.code_ok = False
        self.level = 3
        self.seed = None
        self.custom_on = False
        self.writes: List[int] = []
        self.ended = False
        # wire state
        self._out: List[int] = []         # bytes waiting for the tester to read
        self._tx_block: List[int] = []    # cluster block being sent
        self._tx_i = 0
        self._await_cpl: Optional[int] = None
        self._rx: List[int] = []
        self._queue: List[List[int]] = []
        self._counter = 0x01
        self._pending_ack = False
        self.baud = 10400

    # -- KLine surface -------------------------------------------------------
    def set_baud(self, baud):
        self.baud = baud

    def clear_rx(self):
        pass

    def close(self):
        pass

    def wake_up(self, address=0x01, even_parity=False, tries=3):
        if address != _vc.ADDRESS:
            raise KLineError("no answer at 0x%02X" % address)
        self._queue = [self._ascii_block(self.ident[i:i + 12])
                       for i in range(0, len(self.ident), 12)]
        self._pending_ack = True
        self._start_next()
        return 1281          # keyword 0x01 0x8A

    def read_byte(self) -> int:
        if self._await_cpl is not None:
            raise KLineTimeout("sim: tester read while the cluster waits for "
                               "a complement")
        if self._out:
            return self._out.pop(0)
        if self._tx_block and self._tx_i < len(self._tx_block):
            b = self._tx_block[self._tx_i]
            self._tx_i += 1
            if self._tx_i == len(self._tx_block):      # 0x03: no complement
                self._tx_block = []
                self._tx_done()
            else:
                self._await_cpl = (~b) & 0xFF
            return b
        raise KLineTimeout("sim: nothing to read")

    def read(self, n):
        return bytes(self.read_byte() for _ in range(n))

    def read_complement(self, b):
        got = self.read_byte()
        if got != (~b) & 0xFF:
            raise KLineError("sim: complement mismatch")

    def write_byte(self, b):
        b &= 0xFF
        if self._await_cpl is not None:
            if b != self._await_cpl:
                raise KLineError("sim: tester sent 0x%02X, expected complement "
                                 "0x%02X" % (b, self._await_cpl))
            self._await_cpl = None
            return
        self._rx.append(b)
        if len(self._rx) == 1 or len(self._rx) <= self._rx[0]:
            self._out.append((~b) & 0xFF)             # complement every byte...
            return
        if b != 0x03:                                  # ...but the terminator
            raise KLineError("sim: block end 0x%02X" % b)
        blk, self._rx = self._rx, []
        self._counter = (blk[1] + 1) & 0xFF
        self._on_block(blk[2], blk[3:-1])

    def write_bytes(self, data):
        for b in data:
            self.write_byte(b)

    # -- blocks ----------------------------------------------------------------
    def _block(self, title, body=b"") -> List[int]:
        body = bytes(body)
        blk = [len(body) + 3, self._counter, title] + list(body) + [0x03]
        self._counter = (self._counter + 1) & 0xFF
        return blk

    def _ascii_block(self, text) -> List[int]:
        return [_vc.BT_ASCII, text.encode("latin-1")]

    def _start_next(self):
        if not self._queue:
            return
        title, *rest = self._queue.pop(0)
        self._tx_block = self._block(title, rest[0] if rest else b"")
        self._tx_i = 0
        self._last_title = title

    def _tx_done(self):
        # after a data block the tester ACKs; after ACK/NAK it sends a command
        pass

    def _reply(self, *blocks):
        """Queue the cluster's answer: data blocks, then a closing ACK."""
        self._queue = [list(b) for b in blocks]
        if not self._queue or self._queue[-1][0] not in (_vc.BT_ACK, _vc.BT_NAK):
            self._pending_ack = True
        else:
            self._pending_ack = False
        self._start_next()

    def _on_block(self, title, body):
        if title == _vc.BT_ACK:
            if self._queue:
                self._start_next()
            elif self._pending_ack:
                self._pending_ack = False
                self._queue = [[_vc.BT_ACK]]
                self._start_next()
            return
        if title == _vc.BT_END:
            self.ended = True
            return
        h = self._handle(title, list(body))
        self._reply(*h)

    def _handle(self, title, body):
        ACK, NAK = [_vc.BT_ACK], [_vc.BT_NAK]
        if title == _vc.BT_READ_IDENT:
            return [self._ascii_block(self.ident[i:i + 12])
                    for i in range(0, len(self.ident), 12)]
        if title == _vc.BT_CUSTOM:
            cmd = body[0] if body else None
            if cmd == 0x80:
                self.custom_on = body[1:5] == [1, 2, 3, 4]
                return [ACK]
            if cmd is not None and cmd > 0x80 and not self.custom_on:
                return [NAK]
            if cmd == 0x84:
                v = body[1]
                data = self.version_bytes if v == 0 else bytes([v, 0x19])
                return [[_vc.BT_CUSTOM, data]]
            if cmd == 0x9D:
                self.code_ok = bytes(body[1:5]) == self.unlock_code
                if self.code_ok and not self.require_seed_key:
                    self.level = 7
                return [ACK if self.code_ok else NAK]
            if cmd == 0x96 and body[1] == 0x04:
                return [[_vc.BT_CUSTOM, bytes([self.level])]]
            if cmd == 0x96 and body[1] == 0x01:
                self.seed = bytes([0x00, 0x5A, 0x00, 0xC3, 0x00, 0x17, 0x00,
                                   0xE8, 0x01, 0x00])
                return [[_vc.BT_CUSTOM, self.seed]]
            if cmd == 0x96 and body[1] == 0x02:
                want = _vc.find_key(self.seed, 7) if self.seed else None
                if self.code_ok and want is not None and bytes(body[2:]) == want:
                    self.level = 7
                return [ACK]
            return [NAK]
        unlocked = self.code_ok and self.level == 7
        if title == _vc.BT_READ_EEPROM:
            n, addr = body[0], (body[1] << 8) | body[2]
            if not unlocked:
                return [NAK]
            return [[_vc.BT_READ_EEPROM_RESP, bytes(self.eeprom[addr:addr + n])]]
        if title == _vc.BT_WRITE_EEPROM:
            n, addr = body[0], (body[1] << 8) | body[2]
            data = bytes(body[3:3 + n])
            if not unlocked or addr in self.refuse_write_at:
                return [NAK]
            if addr in self.corrupt_write_at:
                data = bytes((x ^ 0xFF) for x in data)
            self.eeprom[addr:addr + n] = data
            self.writes.append(addr)
            return [[_vc.BT_WRITE_EEPROM_RESP, bytes(body[:4])]]
        return [NAK]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The generic KW1281 command set over the suite's block dialog.

Block titles and bodies are the ones KWHack's working implementation uses
(``KWHack.Core/vendor-kw1281test/BlockTitle.cs`` and ``KW1281Dialog.cs``,
kw1281test lineage, MIT), not a protocol summary from elsewhere:

======  ======================  ===================================================
title   name                    body
======  ======================  ===================================================
0x00    read identification     --                                  -> F6 blocks
0x01    read RAM                count, addr hi, addr lo             -> FE data
0x03    read ROM / EEPROM       count, addr hi, addr lo             -> FD data
0x04    actuator test           one byte (0x00 = next)              -> F5 id hi, id lo
0x05    clear fault codes       --                                  -> FC, then ACK
0x07    read fault codes        --                                  -> FC code hi, lo, status ...
0x10    software coding         coding*2 hi, coding*2 lo (+1 when WSC > 65535), WSC hi, lo
                                                                    -> identification blocks
0x11    basic setting, group 0  --                                  -> F4 raw
0x12    raw data read, group 0  --                                  -> F4 raw
0x19    read EEPROM             count, addr hi, addr lo             -> EF data
0x1A    write EEPROM            count, addr hi, addr lo, data       -> F9 count, addr
0x21    adaptation read         channel                             -> E6 channel, value hi, lo [, cells]
0x22    adaptation test         channel, value hi, value lo         -> E6
0x28    basic setting           group                               -> E7 cells / F4 raw / 02 text
0x29    group reading           group                               -> E7 cells / F4 raw / 02 text
0x2A    adaptation save         channel, value hi, lo, WSC (3)      -> E6
0x2B    login                   code hi, code lo, WSC (3)           -> ACK
======  ======================  ===================================================

An ECU in KW1281 drops the link without traffic; the caller keeps it alive
with :meth:`keepalive` (an ACK exchange) between commands. Qt-free.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..flash import diag as _diag
from ..flash import kwp as _kwp
from ..flash.transport import KLineError

BT_READ_IDENT = 0x00
BT_READ_RAM = 0x01
BT_GROUP_TEXT = 0x02
BT_READ_ROM = 0x03
BT_ACTUATOR = 0x04
BT_CLEAR_FAULTS = 0x05
BT_END = 0x06
BT_READ_FAULTS = 0x07
BT_ACK = 0x09
BT_NAK = 0x0A
BT_CODING = 0x10
BT_BASIC_RAW = 0x11
BT_RAW = 0x12
BT_READ_EEPROM = 0x19
BT_WRITE_EEPROM = 0x1A
BT_ADAPT_READ = 0x21
BT_ADAPT_TEST = 0x22
BT_BASIC = 0x28
BT_GROUP = 0x29
BT_ADAPT_SAVE = 0x2A
BT_LOGIN = 0x2B
BT_ADAPT_RESP = 0xE6
BT_GROUP_RESP = 0xE7
BT_EEPROM_RESP = 0xEF
BT_RAW_RESP = 0xF4
BT_ACTUATOR_RESP = 0xF5
BT_ASCII = 0xF6
BT_WRITE_EEPROM_RESP = 0xF9
BT_FAULTS_RESP = 0xFC
BT_ROM_RESP = 0xFD
BT_RAM_RESP = 0xFE

#: the actuator-test response body that means the sequence is over
ACTUATOR_END = bytes([0x04, 0xAB])


class Refused(Exception):
    """The module answered NAK (a login is missing, or the function does
    not exist on this module)."""


@dataclass
class Adaptation:
    channel: int
    value: int
    cells: List[_diag.Cell] = field(default_factory=list)   # the measuring values some ECUs append
    raw: bytes = b""


@dataclass
class ActuatorStep:
    id: int                 # the 16-bit id the module reported (VAG output-test code scheme on engines)
    raw: bytes
    ended: bool


@dataclass
class GroupReading:
    group: int
    cells: Optional[List[_diag.Cell]] = None   # E7: decoded cells
    raw: Optional[bytes] = None                # F4: raw data (group 0, some modules)
    text: Optional[bytes] = None               # 02: the text-form block
    available: bool = True


def _title(blk: bytes) -> int:
    return blk[2]


def _body(blk: bytes) -> bytes:
    return bytes(blk[3:])


class Kw1281Commands:
    """Commands over a connected :class:`ovtsuite.flash.kwp.Kw1281` dialog."""

    def __init__(self, kw: _kwp.Kw1281, log=None):
        self.kw = kw
        self._log = log or (lambda s: None)

    # ------------------------------------------------------------- helpers
    def _exchange(self, payload, expect: Optional[int] = None) -> bytes:
        """Send one block, receive one, ACK-chaining is the caller's: for the
        single-response commands (adaptation, group, actuator, memory)."""
        self.kw.send_block(list(payload))
        blk = self.kw.receive_block()
        if _title(blk) == BT_NAK:
            raise Refused("module refused block 0x%02X" % payload[0])
        if expect is not None and _title(blk) != expect:
            raise KLineError("KW1281: expected block 0x%02X, got 0x%02X" % (expect, _title(blk)))
        return blk

    def _exchange_all(self, payload) -> List[bytes]:
        """Send one block and receive every block up to the module's ACK/NAK,
        acknowledging each (identification-style answers)."""
        self.kw.send_block(list(payload))
        return self.kw._receive_blocks()

    def keepalive(self):
        self.kw.send_block([BT_ACK])
        self.kw.receive_block()

    # --------------------------------------------------------------- login
    def login(self, code: int, wsc: int) -> bool:
        """Block 0x2B: a 16-bit code and the 3-byte workshop code; True on
        ACK, False on NAK."""
        blocks = self._exchange_all([BT_LOGIN, (code >> 8) & 0xFF, code & 0xFF,
                                     (wsc >> 16) & 0xFF, (wsc >> 8) & 0xFF, wsc & 0xFF])
        ok = bool(blocks) and _title(blocks[-1]) == BT_ACK
        self._log("Login %s" % ("accepted" if ok else "refused"))
        return ok

    # ---------------------------------------------------------- adaptation
    def _adaptation(self, payload) -> Adaptation:
        blk = self._exchange(payload, BT_ADAPT_RESP)
        body = _body(blk)
        if len(body) < 3:
            raise KLineError("KW1281: short adaptation response (%d bytes)" % len(body))
        cells = []
        tail = body[3:]
        if len(tail) >= 3 and len(tail) % 3 == 0:
            try:
                cells = _diag.parse_cells(tail, drop_echo=False)
            except Exception:
                cells = []
        return Adaptation(channel=body[0], value=(body[1] << 8) | body[2], cells=cells, raw=body)

    def adaptation_read(self, channel: int) -> Adaptation:
        return self._adaptation([BT_ADAPT_READ, channel & 0xFF])

    def adaptation_test(self, channel: int, value: int) -> Adaptation:
        return self._adaptation([BT_ADAPT_TEST, channel & 0xFF, (value >> 8) & 0xFF, value & 0xFF])

    def adaptation_save(self, channel: int, value: int, wsc: int) -> Adaptation:
        return self._adaptation([BT_ADAPT_SAVE, channel & 0xFF, (value >> 8) & 0xFF, value & 0xFF,
                                 (wsc >> 16) & 0xFF, (wsc >> 8) & 0xFF, wsc & 0xFF])

    # -------------------------------------------------------------- coding
    def set_coding(self, coding: int, wsc: int) -> Tuple[bool, List[bytes]]:
        """Block 0x10 as KWHack sends it: the coding doubled in a 16-bit
        field whose low bit carries bit 16 of the workshop code. The module
        answers with its identification blocks, from which the caller
        verifies the new coding and WSC; ``(accepted, blocks)``."""
        c2 = (coding * 2) & 0xFFFF
        lo = c2 & 0xFF
        if wsc > 65535:
            lo = (lo + 1) & 0xFF
        blocks = self._exchange_all([BT_CODING, (c2 >> 8) & 0xFF, lo,
                                     ((wsc & 0xFFFF) >> 8) & 0xFF, wsc & 0xFF])
        if len(blocks) == 1 and _title(blocks[0]) == BT_NAK:
            return False, blocks
        return True, [b for b in blocks if _title(b) not in (BT_ACK, BT_NAK)]

    # ------------------------------------------------------- actuator test
    def actuator_next(self) -> ActuatorStep:
        """Block 0x04 0x00: the module switches to the next actuator of its
        sequence and names it with a 16-bit id; ``04 AB`` ends the sequence."""
        blk = self._exchange([BT_ACTUATOR, 0x00], BT_ACTUATOR_RESP)
        body = _body(blk)
        ended = body[:2] == ACTUATOR_END
        aid = ((body[0] << 8) | body[1]) if len(body) >= 2 else -1
        return ActuatorStep(id=aid, raw=body, ended=ended)

    # ------------------------------------------------- groups and settings
    def read_group(self, group: int, basic: bool = False) -> GroupReading:
        """Block 0x29 (or 0x28 for a basic setting); group 0 is the raw
        read 0x12 (0x11)."""
        if group == 0:
            payload = [BT_BASIC_RAW if basic else BT_RAW]
        else:
            payload = [BT_BASIC if basic else BT_GROUP, group & 0xFF]
        self.kw.send_block(payload)
        blk = self.kw.receive_block()
        t = _title(blk)
        if t == BT_NAK:
            return GroupReading(group=group, available=False)
        if t == BT_GROUP_RESP:
            return GroupReading(group=group, cells=_diag.parse_cells(_body(blk), drop_echo=False))
        if t == BT_RAW_RESP:
            return GroupReading(group=group, raw=_body(blk))
        if t == BT_GROUP_TEXT:
            return GroupReading(group=group, text=_body(blk))
        if t == BT_ACK:
            return GroupReading(group=group, available=False)
        raise KLineError("KW1281: unexpected block 0x%02X to a group read" % t)

    # -------------------------------------------------------------- memory
    def _memory(self, title: int, resp: int, address: int, count: int) -> Optional[bytes]:
        try:
            blk = self._exchange([title, count & 0xFF, (address >> 8) & 0xFF, address & 0xFF], resp)
        except Refused:
            return None
        return _body(blk)

    def read_eeprom(self, address: int, count: int) -> Optional[bytes]:
        return self._memory(BT_READ_EEPROM, BT_EEPROM_RESP, address, count)

    def read_rom(self, address: int, count: int) -> Optional[bytes]:
        return self._memory(BT_READ_ROM, BT_ROM_RESP, address, count)

    def read_ram(self, address: int, count: int) -> Optional[bytes]:
        return self._memory(BT_READ_RAM, BT_RAM_RESP, address, count)

    def write_eeprom(self, address: int, data: bytes) -> bool:
        data = bytes(data)
        head = [len(data) & 0xFF, (address >> 8) & 0xFF, address & 0xFF]
        try:
            blocks = self._exchange_all([BT_WRITE_EEPROM] + head + list(data))
        except Refused:
            return False
        blocks = [b for b in blocks if _title(b) not in (BT_ACK, BT_NAK)]
        return (len(blocks) == 1 and _title(blocks[0]) == BT_WRITE_EEPROM_RESP
                and _body(blocks[0])[:3] == bytes(head))

    def read_memory_range(self, kind: str, start: int, length: int, chunk: int = 16,
                          is_stop=None) -> Optional[bytes]:
        """``kind`` eeprom | rom | ram, in ``chunk``-byte reads; None when the
        module refuses the first read."""
        fn = {"eeprom": self.read_eeprom, "rom": self.read_rom, "ram": self.read_ram}[kind]
        out = bytearray()
        addr = start
        while addr < start + length:
            if is_stop is not None and is_stop():
                break
            n = min(chunk, start + length - addr)
            got = fn(addr, n)
            if got is None:
                return None if not out else bytes(out)
            out += got[:n]
            addr += n
        return bytes(out)

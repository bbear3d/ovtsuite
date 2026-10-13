# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 hardware boot mode (C167 bootstrap loader) flash read/write.

Port of KWHack ``EDC15/Edc15BootModeVM.cs`` + ``docs/BootMode-Protocol.md``.
The ECU must be powered up with its boot pin bridged: the C167 ROM BSL then
auto-bauds on a 0x00 byte (28800 baud), answers 0xC5, and takes a 32-byte
stage-1 which pulls in the monitor. Everything after that is the monitor's
command set (PING/PEEK/POKE/READ/WRITE/CALL; ACK 0xB1, OK 0xB2, ERR 0xBE,
NAK 0xBF; addresses 3 bytes LE, counts 2 bytes LE). Flash is read at CPU-bus
0x800000 in 512-byte blocks; writes upload a flash helper to RAM 0xF600 and
CALL it for autoselect / whole-chip erase / 512-byte program.

Boot mode talks to the flash chip directly, so it works on an ECU whose
firmware is gone entirely (the KWP2000 paths need at least the boot stub).
"""
from .transport import KLineError, KLineTimeout
from . import loaders as _loaders

BAUD = 28800
FLASH_SIZE = 0x80000
CHUNK = 0x200
FLASH_CPU_BASE = 0x800000

READY, ACK, OK, ERR, NAK = 0xB0, 0xB1, 0xB2, 0xBE, 0xBF
OP_PING, OP_PEEK, OP_POKE, OP_READ, OP_WRITE, OP_CALL = 0x20, 0x21, 0x22, 0x23, 0x24, 0x25
STAGE1_READY = 0xA5
CPU_C167 = 0xC5

HELPER_RAM = 0xF600
HELPER_ENTRY = 0xF600
PROGRAM_BUF = 0xFC00
HELPER_PROGRAM, HELPER_CHIP_ERASE, HELPER_AUTOSELECT = 0x00, 0x02, 0x06
WRITE_SEG_BASE, READ_SEG_BASE = 0x40, 0x80

READ_MODE_REGS = [(0xFF12, 0xE204), (0xFF0C, 0x04AD), (0xFE18, 0x3803), (0xFE1A, 0x2008),
                  (0xFE1C, 0x0000), (0xFE1E, 0x0000), (0xFF14, 0x040D), (0xFF16, 0x04AD),
                  (0xFF18, 0x0000), (0xFF1A, 0x0000)]
WRITE_MODE_REGS = [(0xFF12, 0xE204), (0xFE18, 0x0000), (0xFE1A, 0x0000), (0xFE1C, 0x0000),
                   (0xFE1E, 0x0000), (0xFF14, 0x0000), (0xFF16, 0x0000), (0xFF18, 0x0000),
                   (0xFF1A, 0x0000), (0xFE1C, 0x4008), (0xFF18, 0x848E), (0xFF0C, 0x04AD)]


class StopRequested(Exception):
    pass


def _xor(data):
    x = 0
    for b in data:
        x ^= b
    return x


class Edc15BootMode:
    def __init__(self, link, log=None):
        self.k = link
        self._log = log or (lambda s: None)

    def log(self, msg):
        self._log(msg)

    @staticmethod
    def _stop(is_stop):
        if is_stop and is_stop():
            raise StopRequested()

    # ---- primitives (every TX byte is echoed; the echo must match) ----
    def _send(self, data):
        data = bytes(data)
        self.k.write_raw(data)
        echo = self.k.read(len(data))
        if echo != data:
            raise KLineError("Boot-mode TX echo mismatch (sent %s, got %s)"
                             % (data[:8].hex(), echo[:8].hex()))

    def _expect(self, want, what):
        got = self.k.read_byte()
        if got != want:
            raise KLineError("%s: expected 0x%02X, got 0x%02X" % (what, want, got))

    def _begin(self, op):
        self._send(bytes([op]))
        st = self.k.read_byte()
        if st == NAK:
            raise KLineError("Monitor rejected command 0x%02X (NAK)" % op)
        if st != ACK:
            raise KLineError("Command 0x%02X: expected ACK, got 0x%02X" % (op, st))

    @staticmethod
    def _addr(a):
        return bytes([a & 0xFF, (a >> 8) & 0xFF, (a >> 16) & 0xFF])

    @staticmethod
    def _count(n):
        return bytes([n & 0xFF, (n >> 8) & 0xFF])

    def ping(self):
        self._begin(OP_PING)
        self._expect(OK, "PING")

    def peek(self, addr):
        self._begin(OP_PEEK)
        self._send(self._addr(addr))
        lo = self.k.read_byte()
        hi = self.k.read_byte()
        self._expect(OK, "PEEK")
        return lo | (hi << 8)

    def poke(self, addr, value):
        self._begin(OP_POKE)
        self._send(self._addr(addr) + self._count(value))
        self._expect(OK, "POKE 0x%04X" % addr)

    def read_block(self, addr, count):
        self._begin(OP_READ)
        self._send(self._addr(addr) + self._count(count))
        data = self.k.read(count)
        ecu_xor = self.k.read_byte()
        self._expect(OK, "READ")
        if ecu_xor != _xor(data):
            raise KLineError("READ 0x%06X checksum mismatch" % addr)
        return data

    def write_block(self, addr, data):
        data = bytes(data)
        self._begin(OP_WRITE)
        self._send(self._addr(addr) + self._count(len(data)))
        self._send(data)
        ecu_xor = self.k.read_byte()
        st = self.k.read_byte()
        if st == ERR:
            raise KLineError("WRITE 0x%06X failed: monitor read-back verify mismatch" % addr)
        if st != OK:
            raise KLineError("WRITE 0x%06X: expected OK, got 0x%02X" % (addr, st))
        if ecu_xor != _xor(data):
            raise KLineError("WRITE 0x%06X checksum mismatch" % addr)

    def call(self, entry, regs8):
        self._begin(OP_CALL)
        self._send(self._addr(entry))
        ctx = bytearray()
        for r in regs8:
            ctx += bytes([r & 0xFF, (r >> 8) & 0xFF])
        self._send(bytes(ctx))
        out = self.k.read(16)
        self._expect(OK, "CALL")
        return [out[i * 2] | (out[i * 2 + 1] << 8) for i in range(8)]

    # ---- handshake ----
    def _handshake(self, is_stop, on_stage):
        self.k.set_baud(BAUD)
        self.k.clear_rx()
        saved = self.k.read_timeout
        self.k.read_timeout = 20.0
        try:
            on_stage("Waiting for the ECU's boot-mode hello (power up with the boot pin "
                     "bridged; up to 20 s)...")
            self.k.write_raw(b"\x00")                       # BSL auto-baud
            echo = self.k.read_byte()
            if echo != 0x00:
                raise KLineError("No boot-mode echo (got 0x%02X): is the boot pin bridged and "
                                 "the ECU freshly powered?" % echo)
            cpu = self.k.read_byte()
            if cpu != CPU_C167:
                self.log("WARNING: CPU version 0x%02X (expected 0x%02X for a C167)" % (cpu, CPU_C167))
        finally:
            self.k.read_timeout = saved
        self._stop(is_stop)
        on_stage("Uploading boot-mode stage-1...")
        self._send(_loaders.boot_stage1())
        self._expect(STAGE1_READY, "Stage-1 ready marker")
        on_stage("Uploading boot-mode monitor...")
        self._send(_loaders.boot_monitor())
        self._expect(READY, "Monitor READY banner")
        self.ping()
        self.log("Boot-mode monitor is up.")

    def connect_read(self, is_stop=None, on_stage=None):
        stage = on_stage or self.log
        self._handshake(is_stop, stage)
        stage("Configuring the external bus for flash read...")
        for addr, val in READ_MODE_REGS:
            self._stop(is_stop)
            self.poke(addr, val)
            back = self.peek(addr)
            if back != val:
                self.log("  register 0x%04X = 0x%04X read back 0x%04X" % (addr, val, back))

    def connect_write(self, is_stop=None, on_stage=None):
        stage = on_stage or self.log
        self._handshake(is_stop, stage)
        stage("Configuring the external bus for flash write...")
        for addr, val in WRITE_MODE_REGS:
            self._stop(is_stop)
            self.poke(addr, val)
        stage("Uploading the flash helper...")
        self.write_block(HELPER_RAM, _loaders.boot_flash_helper())

    # ---- flash ----
    def read_flash(self, on_percent=None, on_stage=None, is_stop=None) -> bytes:
        stage = on_stage or self.log
        stage("Reading flash (boot mode)...")
        out = bytearray()
        before = -1
        for off in range(0, FLASH_SIZE, CHUNK):
            self._stop(is_stop)
            out += self.read_block(FLASH_CPU_BASE + off, CHUNK)
            pct = (off + CHUNK) * 100 // FLASH_SIZE
            if pct != before and on_percent:
                on_percent(pct)
                before = pct
        return bytes(out)

    def erase_and_write_flash(self, image, on_percent=None, on_stage=None, is_stop=None):
        image = bytes(image)
        if len(image) != FLASH_SIZE:
            raise ValueError("boot-mode write needs exactly 0x%X bytes; got %d" % (FLASH_SIZE, len(image)))
        stage = on_stage or self.log
        ident = self.call(HELPER_ENTRY, [HELPER_AUTOSELECT, WRITE_SEG_BASE, READ_SEG_BASE, 1, 0, 0, 0, 0])
        self.log("Flash device ID (autoselect): 0x%04X" % ident[1])
        self._stop(is_stop)
        stage("Erasing whole chip...")
        r = self.call(HELPER_ENTRY, [HELPER_CHIP_ERASE, WRITE_SEG_BASE, READ_SEG_BASE, 0, 0, 0, 0, 0])
        if r[7] != 0:
            raise KLineError("Whole-chip erase failed (helper R15=0x%04X; 0x31 = erase timeout). "
                             "Nothing was written; re-read to check the chip." % r[7])
        stage("Programming flash (boot mode)...")
        before = -1
        for off in range(0, FLASH_SIZE, CHUNK):
            self._stop(is_stop)
            block = image[off:off + CHUNK]
            if any(b != 0xFF for b in block):
                self.write_block(PROGRAM_BUF, block)
                seg = off >> 16
                r = self.call(HELPER_ENTRY, [HELPER_PROGRAM, WRITE_SEG_BASE + seg, READ_SEG_BASE + seg,
                                             off & 0xFFFF, PROGRAM_BUF, CHUNK, 0, 0])
                if r[7] != 0:
                    raise KLineError("Program failed at flash offset 0x%05X (helper R15=0x%04X)"
                                     % (off, r[7]))
            pct = (off + CHUNK) * 100 // FLASH_SIZE
            if pct != before and on_percent:
                on_percent(pct)
                before = pct
        stage("Boot-mode flash write complete.")

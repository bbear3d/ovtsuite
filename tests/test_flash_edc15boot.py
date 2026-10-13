# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 boot-mode (C167 BSL + KWHack monitor) tests against a mock ECU."""
import pytest

from ovtsuite.flash import edc15boot as BM
from ovtsuite.flash import loaders as L
from ovtsuite.flash.transport import KLineTimeout, KLineError


def _xor(d):
    x = 0
    for b in d:
        x ^= b
    return x


class MockBootEcu:
    """Plays the BSL hello, stage-1/monitor upload, then the monitor command
    set over a fake half-duplex serial port (echo of every host byte)."""

    def __init__(self, flash=None):
        self.rx = bytearray()
        self.parse = bytearray()
        self.baudrate = 0
        self.flash = bytearray(flash if flash is not None else bytes((i * 5 + 3) & 0xFF for i in range(BM.FLASH_SIZE)))
        self.ram = {}
        self.regs = {}
        self.state = "bsl"
        self.erased = False
        self.programmed = 0
        self.helper_uploaded = False

    def reset_input_buffer(self):
        self.rx.clear()

    def read(self, n):
        out = bytes(self.rx[:n]); del self.rx[:n]; return out

    def write(self, data):
        self.rx += data
        self.parse += data
        self._consume()

    def _consume(self):
        p = self.parse
        while p:
            if self.state == "bsl":
                assert p[0] == 0x00 and self.baudrate == BM.BAUD
                del p[0]
                self.rx.append(BM.CPU_C167)
                self.state = "stage1"
            elif self.state == "stage1":
                if len(p) < 32:
                    return
                assert bytes(p[:32]) == L.boot_stage1(); del p[:32]
                self.rx.append(BM.STAGE1_READY)
                self.state = "monitor"
            elif self.state == "monitor":
                mon = L.boot_monitor()
                if len(p) < len(mon):
                    return
                assert bytes(p[:len(mon)]) == mon; del p[:len(mon)]
                self.rx.append(BM.READY)
                self.state = "cmd"
            elif self.state == "cmd":
                if not self._cmd():
                    return
            else:
                return

    def _cmd(self):
        p = self.parse
        pend = getattr(self, "_pending", None)
        if pend is None:
            op = p[0]; del p[0]
            if op in (BM.OP_PING, BM.OP_PEEK, BM.OP_POKE, BM.OP_READ, BM.OP_WRITE, BM.OP_CALL):
                self.rx.append(BM.ACK)            # ACK the opcode before any arguments arrive
                if op == BM.OP_PING:
                    self.rx.append(BM.OK)
                else:
                    self._pending = op
            else:
                self.rx.append(BM.NAK)
            return True
        op = pend
        if op == BM.OP_PEEK:
            if len(p) < 3: return False
            a = p[0] | (p[1] << 8) | (p[2] << 16); del p[:3]
            v = self.regs.get(a, 0)
            self.rx += bytes([v & 0xFF, v >> 8, BM.OK])
        elif op == BM.OP_POKE:
            if len(p) < 5: return False
            a = p[0] | (p[1] << 8) | (p[2] << 16); v = p[3] | (p[4] << 8); del p[:5]
            self.regs[a] = v
            self.rx.append(BM.OK)
        elif op == BM.OP_READ:
            if len(p) < 5: return False
            a = p[0] | (p[1] << 8) | (p[2] << 16); n = p[3] | (p[4] << 8); del p[:5]
            assert a >= BM.FLASH_CPU_BASE
            d = bytes(self.flash[a - BM.FLASH_CPU_BASE:a - BM.FLASH_CPU_BASE + n])
            self.rx += d + bytes([_xor(d), BM.OK])
        elif op == BM.OP_WRITE:
            if len(p) < 5: return False
            n = p[3] | (p[4] << 8)
            if len(p) < 5 + n: return False
            a = p[0] | (p[1] << 8) | (p[2] << 16); d = bytes(p[5:5 + n]); del p[:5 + n]
            self.ram[a] = d
            if a == BM.HELPER_RAM:
                self.helper_uploaded = d == L.boot_flash_helper()
            self.rx += bytes([_xor(d), BM.OK])
        elif op == BM.OP_CALL:
            if len(p) < 3 + 16: return False
            entry = p[0] | (p[1] << 8) | (p[2] << 16)
            regs = [p[3 + i * 2] | (p[4 + i * 2] << 8) for i in range(8)]
            del p[:19]
            assert entry == BM.HELPER_ENTRY and self.helper_uploaded
            out = list(regs)
            sel = regs[0]
            if sel == BM.HELPER_AUTOSELECT:
                out[1] = 0x2223                       # Am29F400BT id
            elif sel == BM.HELPER_CHIP_ERASE:
                self.flash[:] = b"\xFF" * BM.FLASH_SIZE; self.erased = True; out[7] = 0
            elif sel == BM.HELPER_PROGRAM:
                assert self.erased
                seg = regs[1] - BM.WRITE_SEG_BASE
                off = (seg << 16) | regs[3]
                src = self.ram[regs[4]]
                assert len(src) == regs[5]
                for i, b in enumerate(src):
                    self.flash[off + i] &= b
                self.programmed += 1
                out[7] = 0
            ctx = bytearray()
            for r in out:
                ctx += bytes([r & 0xFF, r >> 8])
            self.rx += bytes(ctx) + bytes([BM.OK])
        self._pending = None
        return True


class _Link:
    def __init__(self, ecu):
        self.port = ecu; self._t = 8.0
    @property
    def read_timeout(self): return self._t
    @read_timeout.setter
    def read_timeout(self, v): self._t = v
    def set_baud(self, b): self.port.baudrate = b
    def clear_rx(self): self.port.reset_input_buffer()
    def read_byte(self):
        b = self.port.read(1)
        if not b: raise KLineTimeout("timeout")
        return b[0]
    def read(self, n):
        out = bytearray()
        while len(out) < n:
            c = self.port.read(n - len(out))
            if not c: raise KLineTimeout("timeout")
            out += c
        return bytes(out)
    def write_raw(self, d): self.port.write(d)


def test_boot_read_roundtrip():
    ecu = MockBootEcu()
    bm = BM.Edc15BootMode(_Link(ecu))
    stages = []
    bm.connect_read(on_stage=stages.append)
    pct = []
    img = bm.read_flash(on_percent=pct.append)
    assert img == bytes(ecu.flash) and pct[-1] == 100
    assert ecu.regs[0xFF12] == 0xE204 and ecu.regs[0xFE18] == 0x3803


def test_boot_write_erases_and_programs_nonblank_blocks():
    ecu = MockBootEcu()
    bm = BM.Edc15BootMode(_Link(ecu))
    bm.connect_write()
    image = bytearray(bytes((i * 3 + 1) & 0xFF for i in range(BM.FLASH_SIZE)))
    image[0x10000:0x10400] = b"\xFF" * 0x400            # two blank blocks are skipped
    bm.erase_and_write_flash(bytes(image))
    assert ecu.erased and bytes(ecu.flash) == bytes(image)
    assert ecu.programmed == BM.FLASH_SIZE // BM.CHUNK - 2
    assert ecu.regs[0xFE1C] == 0x4008 and ecu.regs[0xFF18] == 0x848E


def test_boot_write_rejects_wrong_size():
    ecu = MockBootEcu()
    bm = BM.Edc15BootMode(_Link(ecu))
    bm.connect_write()
    with pytest.raises(ValueError):
        bm.erase_and_write_flash(b"\x00" * 100)


def test_boot_stop_during_read():
    ecu = MockBootEcu()
    bm = BM.Edc15BootMode(_Link(ecu))
    bm.connect_read()
    n = [0]
    def stop():
        n[0] += 1
        return n[0] > 5
    with pytest.raises(BM.StopRequested):
        bm.read_flash(is_stop=stop)

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 flash/EEPROM state-machine tests against a mock ECU.

The mock plays the whole EDC15 dance: a KW1281 wakeup at 9600 (two ident
blocks + ACK, byte-complement acks, EndCommunication), the KWP2000 wakeup at
10400, session/security, a loader upload to 0x40E000, and then -- depending on
which loader was uploaded -- the whole-chip flash loader's read/erase/program
protocol, the sector loader's B2/B0/36/23 commands, or the EEPROM loader's
A6/A9/AA commands. Every frame the port emits is parsed byte-exactly.
"""
import pytest

from ovtsuite.flash import edc15 as E
from ovtsuite.flash import loaders as L
from ovtsuite.flash.transport import KLineTimeout, adjust_parity


def _ck(data):
    return sum(data) & 0xFF


class MockEdc15:
    def __init__(self, flash=None, eeprom=None, soft_brick=False, refuse_speed=False,
                 seed=None, irom=None, size=E.FLASH_SIZE, prog_window_readable=True):
        self.seed = seed              # 4-byte seed -> the ECU demands a real LVL41 key
        self.rx = bytearray()
        self.parse = bytearray()
        self.baudrate = 9600
        self.timeout = 8.0
        self.dtr = False
        self.rts = False
        self.break_condition = False
        self._broke = False
        #: physical flash size (0x80000 Am29F400 / 0x100000 Am29F800) and its
        #: erase-sector table. Addresses from the tester are mapped to this chip
        #: by ``chip = cpu & (size - 1)`` wherever a flash window decodes them,
        #: which is exactly the real part's address aliasing.
        self.size = size
        #: False reproduces a real 512 KB ECU (bench log): the 0x200000
        #: programming window is write-only, a read returns the open-bus
        #: pattern <segment> 00 repeated, not flash.
        self.prog_window_readable = prog_window_readable
        self.sectors = (E.SECTORS_1M if size == 0x100000 else E.SECTORS)
        # the ``i >> 13`` term makes the content vary across the chip (the two
        # 512 KB halves differ, as real flash halves do), so the size probe
        # isn't fooled by a byte pattern that happens to repeat every 256 B.
        self.flash = bytearray(flash if flash is not None else bytes(
            (i * 7 + 1 + (i >> 13)) & 0xFF for i in range(size)))
        self.eeprom = bytearray(eeprom if eeprom is not None else bytes(
            (i * 3) & 0xFF for i in range(512)))
        # The C167 internal boot ROM, as the running ECU maps it at
        # E.IROM_BASE. Default content is shaped like a real dump: a leading
        # vector table of JMPS entries, then pseudo-code.
        self.irom = bytearray(irom if irom is not None else (
            bytes([0xFA, 0x00, 0x4C, 0x86] * 64)
            + bytes((i * 31 + 7) & 0xFF for i in range(E.IROM_SIZE - 256))))
        self.soft_brick = soft_brick
        self.refuse_speed = refuse_speed
        # state
        self.mode = "idle"            # idle | kw1281 | kwp | loader
        self.kw1281_ended = False
        self.loader = None            # "flash" | "sector" | "eeprom" (recognised by size)
        self.loader_buf = bytearray()
        self.loader_len = 0
        self.stage = None
        self.counter = 0
        self.expect_raw = 0           # raw bytes the loader is waiting for (eeprom pairs)
        self.raw_pairs_page = None
        self.log = []
        self.erased = []
        self.closed = 0

    # ---- pyserial-ish ----
    def flush(self):
        pass

    def close(self):
        pass

    def __setattr__(self, k, v):
        if k == "break_condition" and v:
            object.__setattr__(self, "_broke", True)
        object.__setattr__(self, k, v)

    def reset_input_buffer(self):
        self.rx.clear()
        if self._broke:
            self._broke = False
            self.parse.clear()
            if self.baudrate == 9600 and not self.soft_brick and not self.kw1281_ended:
                self.rx += bytes([0x55, 0x01, 0x8A])          # KW 1281
                self.mode = "kw1281-kw"
            elif self.baudrate == 10400:
                self.rx += bytes([0x55, 0xEF, 0x8F])          # KW 2031
                self.mode = "kwp-kw"
            else:
                self.rx += bytes([0x95])                      # soft-brick garbage at 9600
                self.mode = "idle"

    def read(self, n):
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.rx += data                                       # echo
        self.parse += data
        self._consume()

    # ---- protocol ----
    def _consume(self):
        p = self.parse
        while p:
            if self.mode == "kw1281-kw":
                # complement of keyword msb 0x8A -> 0x75; then send ident blocks
                if p[0] == (~0x8A) & 0xFF:
                    del p[0]
                    self.mode = "kw1281"
                    self.counter = 1
                    self._send_block(0xF6, b"038906019AB  ")
                    self.pending_blocks = [(0xF6, b"1.9l R4 EDC  ")]
                    continue
                return
            if self.mode == "kw1281":
                if not self._kw1281_byte(p[0]):
                    return
                del p[0]
                continue
            if self.mode == "kwp-kw":
                if p[0] == (~0x8F) & 0xFF:
                    del p[0]
                    self.rx += bytes([(~adjust_parity(0x01, False)) & 0xFF])
                    self.mode = "kwp"
                    continue
                return
            if self.mode == "kwp":
                if not self._kwp_frame():
                    return
                continue
            if self.mode == "loader":
                if not self._loader_frame():
                    return
                continue
            return

    # -- KW1281 block dialog: the mock ECU sends blocks and reads complements --
    def _send_block(self, title, data):
        blk = bytes([len(data) + 3, self.counter, title]) + bytes(data)
        self.counter = (self.counter + 1) & 0xFF
        self._out_block = list(blk)
        self._out_idx = 0
        self.rx.append(self._out_block[0])
        self._in_block = None

    def _kw1281_byte(self, b):
        # sending a block: expect complement of each byte we sent
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
        # receiving a block from the tester: ack each byte with its complement
        if self._in_block is None:
            self._in_block = bytearray()
        if b == 0x03 and len(self._in_block) >= 3 and len(self._in_block) == self._in_block[0]:
            blk = bytes(self._in_block)
            self._in_block = None
            assert blk[1] == self.counter, "tester block counter"
            self.counter = (self.counter + 1) & 0xFF
            title = blk[2]
            if title == 0x09:                                 # ACK -> next ident block or ACK
                if self.pending_blocks:
                    t, d = self.pending_blocks.pop(0)
                    self._send_block(t, d)
                else:
                    self._send_block(0x09, b"")
            elif title == 0x06:                               # EndCommunication
                self.kw1281_ended = True
                self.mode = "idle"
            return True
        self._in_block.append(b)
        self.rx.append((~b) & 0xFF)
        return True

    # -- KWP2000 --
    def _kwp_frame(self):
        p = self.parse
        fmt = p[0]
        hdr = 1
        if fmt & 0x80:
            hdr += 2
        if (fmt & 63) == 0:
            if len(p) < hdr + 1:
                return False
            n = p[hdr]
            hdr += 1
        else:
            n = fmt & 63
        total = hdr + n + 1
        if len(p) < total:
            return False
        frame = bytes(p[:total])
        del p[:total]
        assert frame[-1] == _ck(frame[:-1]), "KWP checksum"
        addressed = bool(fmt & 0x80)
        svc = frame[hdr]
        body = frame[hdr + 1:-1]
        self.log.append((svc, body))
        if svc == 0x10:
            if len(body) == 2 and self.refuse_speed:
                self._reply(0x7F, bytes([0x10, 0x12]), addressed)
            else:
                if len(body) == 2:
                    self.baudrate = {0x50: 38400, 0x87: 124800}[body[1]]
                self._reply(0x50, body[:1], addressed)
        elif svc == 0x27:
            if body[0] == 0x41:
                self._reply(0x67, bytes([0x41]) + (self.seed or b"\x00\x00"), addressed)
            else:
                if self.seed:
                    from ovtsuite.flash.crypto import compute_lvl41_key
                    want = compute_lvl41_key(E.VARIANTS["V"][0], E.KEY3, self.seed)
                    if bytes(body[1:]) != want:
                        self._reply(0x7F, bytes([0x27, 0x35]), addressed)
                        return True
                self._reply(0x67, bytes([0x42, 0x34]), addressed)
        elif svc == 0x34:
            assert body[:3] == bytes([0x40, 0xE0, 0x00])
            self.loader_len = (body[4] << 16) | (body[5] << 8) | body[6]
            self.loader_buf = bytearray()
            self._reply(0x74, bytes([0xFF]), addressed)
        elif svc == 0x36 and self.loader is None:
            self.loader_buf += body
            self._reply(0x76, b"", addressed)
        elif svc == 0x31:
            self.loader = self._classify(bytes(self.loader_buf))
            if self.loader == "flash":
                # the V post-loader handshake IS a raw startRoutine 31 02 08 00 00 0F BF FF
                assert not addressed and bytes(frame[:-1]) == E.VARIANTS["V"][1]
            else:
                assert body == b"\x02"
            self.mode = "loader"
            self._reply(0x7F, bytes([0x31, 0x22]), addressed)
        else:
            raise AssertionError("unexpected KWP service 0x%02X" % svc)
        return True

    def _classify(self, buf):
        if buf[:1016] == L.flash_loader() and len(buf) == 1016 + 8:
            return "flash"                                    # + 8 post-loader config bytes
        if buf == L.sector_loader():
            return "sector"
        if buf == L.eeprom_loader():
            return "eeprom"
        raise AssertionError("unknown loader upload (%d bytes)" % len(buf))

    def _reply(self, svc, body, addressed):
        n = len(body) + 1
        if addressed:
            out = bytearray([0x80 | n, 0xF1, 0x01, svc]) + bytes(body)
        else:
            out = bytearray([n, svc]) + bytes(body)
        out.append(_ck(out))
        self.rx += out

    # -- running loader --
    def _loader_frame(self):
        p = self.parse
        if self.loader == "flash":
            return self._flash_loader_frame()
        if self.expect_raw:
            if len(p) < self.expect_raw:
                return False
            raw = bytes(p[:self.expect_raw])
            del p[:self.expect_raw]
            self.expect_raw = 0
            base = 0x100 if self.raw_pairs_page else 0
            for i in range(0, len(raw), 2):
                self.eeprom[base + raw[i]] = raw[i + 1]
            self.rx += bytes([0x01, 0x76, 0x77])
            return True
        n = p[0]
        if len(p) < n + 2:
            return False
        frame = bytes(p[:n + 2])
        del p[:n + 2]
        assert frame[-1] == _ck(frame[:-1])
        svc, data = frame[1], frame[2:-1]
        if svc == 0xA2:
            self.closed += 1
            self.rx += bytes([0x01, 0x76, 0x77, 0x55])
            self.mode = "idle"
        elif svc == 0xB2:
            cs = (data[0] << 16) | (data[1] << 8) | data[2]
            ce = (data[3] << 16) | (data[4] << 8) | data[5]
            c = E.sector_checksum(self._read_bytes(cs, ce - cs))
            self._lreply(0xB2, bytes([c >> 8, c & 0xFF]))
        elif svc == 0xB0:
            a = self._chip((data[0] << 16) | (data[1] << 8) | data[2])
            for s, e in self.sectors:
                if s == a:
                    self.flash[s:e] = b"\xFF" * (e - s)
                    self.erased.append(s)
                    break
            else:
                raise AssertionError("erase at non-sector 0x%X" % a)
            self._lreply(0x76, b"")
            self._lreply(0x76, b"")                            # post-erase ACK
        elif svc == 0x36 and self.loader == "sector":
            a = self._chip((data[0] << 16) | (data[1] << 8) | data[2])
            payload = data[3:]
            for i, b in enumerate(payload):
                self.flash[a + i] &= b                          # NOR: only 1->0
            self._lreply(0x76, b"")
        elif svc == 0x23:
            cpu = (data[0] << 16) | (data[1] << 8) | data[2]
            self._lreply(0x36, self._cpu_read(cpu, data[3]))
        elif svc == 0xA6:
            self.rx += bytes([0x01, 0x76, 0x77]) + bytes(self.eeprom) + bytes([0x01, 0x76, 0x77])
        elif svc in (0xA9, 0xAA):
            self.raw_pairs_page = svc == 0xAA
            self.expect_raw = data[0] * 2
            self.rx += bytes([0x01, 0x76, 0x77])
        else:
            raise AssertionError("unexpected loader command 0x%02X" % svc)
        return True

    # flash windows the global chip-select decodes: the 512 KB read window at
    # 0x080000 and the 1/2 MB programming window at 0x200000. Within either,
    # the chip aliases by ``cpu & (size - 1)`` -- exactly how a smaller part
    # ignores the high address line a bigger one decodes.
    _FLASH_WINDOWS = ((0x080000, 0x100000), (0x200000, 0x400000))

    def _chip(self, cpu):
        for lo, hi in self._FLASH_WINDOWS:
            if lo <= cpu < hi:
                return cpu & (self.size - 1)
        raise AssertionError("address 0x%06X not in a flash window" % cpu)

    def _read_bytes(self, cpu, n):
        """What a read of ``n`` bytes at ``cpu`` returns."""
        if cpu >= 0x200000 and not self.prog_window_readable:
            return bytes([(cpu >> 16) & 0xFF, 0x00] * ((n + 1) // 2))[:n]
        o = self._chip(cpu)
        return bytes(self.flash[o:o + n])

    def _cpu_read(self, cpu, n):
        """Serve the CPU-bus address space the running ECU maps: the internal
        boot ROM at IROM_BASE, and the external flash at either window."""
        if E.IROM_BASE <= cpu < E.IROM_BASE + E.IROM_SIZE:
            o = cpu - E.IROM_BASE
            return bytes(self.irom[o:o + n])
        return self._read_bytes(cpu, n)

    def _lreply(self, svc, data):
        out = bytearray([1 + len(data), svc]) + bytes(data)
        out.append(_ck(out))
        self.rx += out

    def _flash_loader_frame(self):
        p = self.parse
        n = p[0]
        if len(p) < n + 2:
            return False
        frame = bytes(p[:n + 2])
        del p[:n + 2]
        assert frame[-1] == _ck(frame[:-1])
        svc, data = frame[1], frame[2:-1]
        if svc == 0x23:
            chunk = self._read_bytes((data[0] << 16) | (data[1] << 8) | data[2], data[3])
            out = bytearray([len(chunk) + 1, 0x36]) + chunk
            out.append(_ck(out))
            self.rx += out
        elif svc == 0xA5:
            self.flash[:] = b"\xFF" * self.size
            self.erased.append("chip")
            self.rx += bytes([0x01, 0x76, 0x77, 0x01, 0x76, 0x77])
        elif svc == 0x36:
            a = self._chip((data[0] << 16) | (data[1] << 8) | data[2])
            payload = data[3:]
            for i, b in enumerate(payload):
                self.flash[a + i] &= b
            self.rx += bytes([0x01, 0x76, 0x77])
        elif svc == 0xA2:
            self.closed += 1
            self.rx += bytes([0x55])
            self.mode = "idle"
        else:
            raise AssertionError("unexpected flash-loader command 0x%02X" % svc)
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
        lsb = self.read_byte(); msb = self.read_byte()
        version = ((msb & 0x7F) << 7) + (lsb & 0x7F)
        self.write_byte((~msb) & 0xFF)
        if version >= 2000:
            self.read_complement(adjust_parity(address, even_parity))
        return version


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda s: None)


def _make(**kw):
    ecu = MockEdc15(**kw)
    return E.Edc15Flash(_Link(ecu)), ecu


# -------------------------------------------------------------- loaders
def test_patched_loaders_checksum_to_target():
    for f in (L.sector_loader, L.eeprom_loader):
        data = f()
        assert len(data) % 8 == 0
        assert L.loader_checksum(data) == (0xEFCD, 0x8631)
        end = 0xE000 + len(data)
        assert data[0x0E] == end & 0xFF and data[0x0F] == end >> 8
    assert len(L.flash_loader()) == 1016


def test_sector_checksum_matches_reference_shape():
    assert E.sector_checksum(b"") == 0
    assert E.sector_checksum(b"\x01") == 1
    assert E.sector_checksum(b"\x01\x01") == 3          # rol(1)=2, +1
    # wraps at 16 bits and rotates the top bit around
    acc = 0
    for b in b"\xFF" * 40:
        acc = (((acc << 1) | (acc >> 15)) + b) & 0xFFFF
    assert E.sector_checksum(b"\xFF" * 40) == acc


# -------------------------------------------------------------- flash read
def test_read_flash_roundtrip_speed_negotiated():
    flash, ecu = _make()
    pct = []
    flash.connect("V", "High", loader="flash")
    assert ecu.baudrate == 124800 and flash._fast
    img = flash.read_flash(on_percent=pct.append)
    assert img == bytes(ecu.flash)
    assert len(img) == E.FLASH_SIZE
    assert pct[-1] == 100 and ecu.closed == 1
    assert ecu.kw1281_ended


def test_read_flash_low_speed_no_negotiation():
    flash, ecu = _make()
    flash.connect("V", "Low", loader="flash")
    assert ecu.baudrate == 10400
    assert not any(svc == 0x10 and len(b) == 2 for svc, b in ecu.log)


def test_speed_refused_stays_slow():
    flash, ecu = _make(refuse_speed=True)
    flash.connect("V", "Medium", loader="flash")
    assert ecu.baudrate == 10400 and not flash._fast
    assert flash.read_flash() == bytes(ecu.flash)


# -------------------------------------------------------------- flash write
def test_per_sector_write_only_changed_sectors():
    flash, ecu = _make()
    image = bytearray(ecu.flash)
    image[0x25000:0x25010] = b"\x00" * 16             # sector 2
    image[0x7A100] ^= 0xFF                             # sector 9 (8 KB)
    flash.connect("Auto", "Medium", loader="sector")
    stages = []
    written = flash.write_flash_sectors(bytes(image), on_stage=stages.append)
    assert written == [2, 9]
    assert ecu.erased == [0x20000, 0x7A000]
    assert bytes(ecu.flash) == bytes(image)
    assert any("Verify passed" in s for s in stages)
    assert ecu.closed == 1


def test_per_sector_write_nothing_to_do():
    flash, ecu = _make()
    flash.connect("V", "Low", loader="sector")
    assert flash.write_flash_sectors(bytes(ecu.flash)) == []
    assert ecu.erased == []


def test_per_sector_force_full_rewrites_all():
    flash, ecu = _make()
    image = bytes((i * 11 + 5) & 0xFF for i in range(E.FLASH_SIZE))
    flash.connect("V", "Low", loader="sector")
    written = flash.write_flash_sectors(image, force_full=True)
    assert written == list(range(len(E.SECTORS)))
    assert bytes(ecu.flash) == image


def test_whole_chip_write():
    flash, ecu = _make()
    image = bytes((i * 5 + 9) & 0xFF for i in range(E.FLASH_SIZE))
    flash.connect("V", "Low", loader="flash")
    pct = []
    flash.write_flash_full(image, on_percent=pct.append)
    assert ecu.erased == ["chip"]
    assert bytes(ecu.flash) == image
    assert pct[-1] == 100


def test_write_rejects_short_image():
    flash, ecu = _make()
    flash.connect("V", "Low", loader="sector")
    with pytest.raises(ValueError):
        flash.write_flash_sectors(b"\x00" * 1000)


# -------------------------------------------------------------- EEPROM
def test_eeprom_read():
    flash, ecu = _make()
    flash.connect("V", "Low", loader="eeprom")
    ee = flash.read_eeprom()
    assert ee == bytes(ecu.eeprom) and len(ee) == 512
    assert ecu.closed == 1


def test_eeprom_write_diff_and_verify():
    flash, ecu = _make()
    new = bytearray(ecu.eeprom)
    new[0x10] = 0xAA                                   # page 0
    new[0x1BF:0x1C3] = b"\x40\x42\x0F\x00"             # page 1 (odometer)
    flash.connect("V", "Low", loader="eeprom")
    back, changed = flash.write_eeprom(bytes(new))
    assert changed == 5 and back == bytes(new)
    assert bytes(ecu.eeprom) == bytes(new)


def test_eeprom_info_decodes():
    ee = bytearray(512)
    ee[0x12E:0x130] = (1234).to_bytes(2, "little")
    ee[0x140:0x151] = b"WVWZZZ1JZXW000001"
    ee[0x1BF:0x1C3] = (123456 * 100).to_bytes(4, "little")
    ee[0x1B0] = ee[0x1DE] = 0x60
    info = E.eeprom_info(ee)
    assert info["skc"] == 1234 and info["vin"] == "WVWZZZ1JZXW000001"
    assert abs(info["odometer_km"] - 123456) < 1e-6 and info["immo_on"] is False


# -------------------------------------------------------------- recovery
def test_soft_brick_probe_then_recovery():
    flash, ecu = _make(soft_brick=True)
    flash.connect("V", "High", loader="sector")
    assert flash.recovered
    # recovery session sends only 0x85 (no 0x89) and never negotiates speed
    subs = [b for svc, b in ecu.log if svc == 0x10]
    assert subs == [b"\x85"]
    assert ecu.baudrate == 10400


def test_stop_during_read():
    flash, ecu = _make()
    flash.connect("V", "Low", loader="flash")
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 3
    with pytest.raises(E.StopRequested):
        flash.read_flash(is_stop=stop)
    assert ecu.closed == 1                              # loader told to stop anyway


def test_security_with_real_seed_key():
    flash, ecu = _make(seed=bytes([0x12, 0x34, 0x56, 0x78]))
    flash.connect("V", "Low", loader="eeprom")
    assert flash.read_eeprom() == bytes(ecu.eeprom)


def test_wrong_variant_key_is_rejected_then_auto_falls_through():
    # P's key is wrong for an ECU that wants V's; Auto tries V first so it just works,
    # while forcing P fails at security access.
    flash, ecu = _make(seed=bytes([0xA1, 0xB2, 0xC3, 0xD4]))
    with pytest.raises(Exception):
        flash.connect("P", "Low", loader="eeprom")
    flash2, ecu2 = _make(seed=bytes([0xA1, 0xB2, 0xC3, 0xD4]))
    flash2.connect("Auto", "Low", loader="eeprom")
    assert flash2.variant == "V"


# ------------------------------------------------- internal ROM over K-line
def test_read_irom_reads_the_c167_internal_rom_without_boot_mode():
    """The running ECU already has its internal boot ROM mapped (it executes
    out of it), and the sector loader's Cmd23 takes an arbitrary CPU-bus
    address, so the ROM comes back over the ordinary K-line session -- no boot
    pin and no bus reconfiguration."""
    f, ecu = _make()
    f.connect("V", "Low", loader="sector")
    got = f.read_irom()
    assert got == bytes(ecu.irom)
    assert len(got) == E.IROM_SIZE


def test_read_cpu_range_addresses_the_bus_not_the_flash_offset():
    f, ecu = _make()
    f.connect("V", "Low", loader="sector")
    # the same bytes reachable two ways: as a flash offset, and as the CPU-bus
    # address the running ECU maps that offset at
    assert (f.read_flash_range(0x12340, 32)
            == f.read_cpu_range(E.SECTOR_READ_BASE + 0x12340, 32)
            == bytes(ecu.flash[0x12340:0x12340 + 32]))
    # and the ROM window is a different place entirely
    assert f.read_cpu_range(E.IROM_BASE + 0x1584, 16) == bytes(ecu.irom[0x1584:0x1584 + 16])


def test_cpu_reads_need_the_sector_loader():
    f, _ = _make()
    f.connect("V", "Low", loader="flash")
    with pytest.raises(Exception):
        f.read_cpu_range(E.IROM_BASE, 16)
    with pytest.raises(Exception):
        f.read_irom()


def test_read_irom_reports_progress_and_can_be_stopped():
    f, _ = _make()
    f.connect("V", "Low", loader="sector")
    seen = []
    with pytest.raises(E.StopRequested):
        f.read_irom(on_percent=seen.append, is_stop=lambda: len(seen) >= 3)
    assert seen and max(seen) < 100


# ------------------------------------------------- 512 KB vs 1 MB detection
def test_detect_layout_512_with_sector_loader():
    f, _ = _make()                                    # default 512 KB part
    f.connect("V", "Low", loader="sector")
    assert f.detect_layout() is E.LAYOUT_512
    assert f.layout.size == 0x80000


def test_detect_layout_512_with_flash_loader():
    f, _ = _make()
    f.connect("V", "Low", loader="flash")
    assert f.detect_layout() is E.LAYOUT_512


def test_detect_layout_1m_with_sector_loader():
    f, _ = _make(size=0x100000)
    f.connect("V", "Low", loader="sector")
    assert f.detect_layout() is E.LAYOUT_1M
    assert f.layout.size == 0x100000


def test_detect_layout_1m_with_flash_loader():
    f, _ = _make(size=0x100000)
    f.connect("V", "Low", loader="flash")
    assert f.detect_layout() is E.LAYOUT_1M


def test_detect_is_signature_free_blank_lower_bank():
    """A 1 MB part whose lower bank is blank (0xFF) still detects as 1 MB:
    the top sector (populated) differs from its blank lower-half alias."""
    flash = bytearray(b"\xFF" * 0x80000 + bytes((i * 7 + 1) & 0xFF for i in range(0x80000)))
    f, _ = _make(size=0x100000, flash=bytes(flash))
    f.connect("V", "Low", loader="sector")
    assert f.detect_layout() is E.LAYOUT_1M


# ------------------------------------------------- 1 MB flash read / write
def test_read_flash_1m_roundtrip():
    f, ecu = _make(size=0x100000)
    f.connect("V", "High", loader="flash")
    f.detect_layout()
    img = f.read_flash()
    assert img == bytes(ecu.flash) and len(img) == 0x100000
    assert ecu.closed == 1


def test_per_sector_write_1m_force_full():
    f, ecu = _make(size=0x100000)
    image = bytes((i * 11 + 5) & 0xFF for i in range(0x100000))
    f.connect("V", "Low", loader="sector")
    f.detect_layout()
    written = f.write_flash_sectors(image, force_full=True)
    assert written == list(range(len(E.SECTORS_1M)))
    assert bytes(ecu.flash) == image


def test_per_sector_write_1m_only_changed():
    f, ecu = _make(size=0x100000)
    image = bytearray(ecu.flash)
    image[0x90000:0x90010] = b"\x00" * 16             # a 64 KB sector (index 9)
    image[0xFD000] ^= 0xFF                            # the top 16 KB boot sector (index 18)
    f.connect("V", "Medium", loader="sector")
    f.detect_layout()
    written = f.write_flash_sectors(bytes(image))
    assert written == [9, 18]
    assert ecu.erased == [0x90000, 0xFC000]
    assert bytes(ecu.flash) == bytes(image)


def test_write_1m_rejects_512k_image():
    f, _ = _make(size=0x100000)
    f.connect("V", "Low", loader="sector")
    f.detect_layout()
    with pytest.raises(ValueError):
        f.write_flash_sectors(b"\x00" * 0x80000)      # a 512 KB file on a 1 MB ECU


# ----------------------- regression: real 512 KB ECU, write-only 0x200000 window
def test_detect_512_when_programming_window_is_open_bus_flash_loader():
    """Bench log: reads at 0x2xxxxx returned '<seg> 00' repeated. The first
    1 MB detector saw two different patterns and called the ECU 1 MB."""
    f, ecu = _make(prog_window_readable=False)
    f.connect("V", "Low", loader="flash")
    assert f.detect_layout() is E.LAYOUT_512
    assert f.read_flash() == bytes(ecu.flash)


def test_detect_512_when_programming_window_is_open_bus_sector_loader():
    f, _ = _make(prog_window_readable=False)
    f.connect("V", "Low", loader="sector")
    assert f.detect_layout() is E.LAYOUT_512


def test_read_refuses_a_pattern_image():
    f, ecu = _make(prog_window_readable=False)
    f.connect("V", "Low", loader="flash")
    f.layout = E.LAYOUT_1M                    # forced wrong layout: reads the open bus
    with pytest.raises(Exception, match="no flash data"):
        f.read_flash()

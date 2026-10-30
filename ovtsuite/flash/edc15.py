# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC15 external-flash and EEPROM read/write over K-line.

Ported from KWHack ``EDC15/Edc15FlashVM.cs`` + ``EDC15/Edc15VM.cs`` (both
bench-validated). The EDC15 is a C167 with either a 512 KB Am29F400BT or a 1 MB
Am29F800BT; :class:`FlashLayout` captures the difference and
:meth:`Edc15Flash.detect_layout` picks it on the ECU (see docs/edc15-1mb.md).
Everything is done through a small RAM loader uploaded over KWP2000:

* connect: 5-baud slow init at 9600 -> KW1281 identification -> EndCommunication
  -> a second slow init at 10400 that answers with KWP2000; then
  startDiagnosticSession 0x89 + 0x85, securityAccess 0x41 (LVL41 key, usually
  "already unlocked"), optional speed negotiation, loader upload to 0x40E000.
* soft-bricked ECUs (stuck in the KWP2000 boot stub, answering the 9600 pulse
  with sync 0x95/0xB5 or a non-1281 keyword) are detected and driven through the
  recovery path (0x85 only, no KW1281 prerequisite) automatically.
* ``read_flash``: whole-chip loader, 254-byte packets, over the whole part
  (512 KB at the 0x080000 window, 1 MB at the 0x200000 window).
* ``write_flash_sectors`` (default write): per-sector loader; the loader's
  CmdB2 checksum is compared against the image per sector and only differing
  sectors are erased (CmdB0) and programmed (Cmd36, 128-byte chunks).
* ``write_flash_full``: whole-chip loader erase (0xA5) + program both blocks.
* ``read_eeprom`` / ``write_eeprom``: EEPROM loader, 0xA6 dump (512 bytes),
  0xA9/0xAA batched page writes, in-session read-back verify.

Speeds: Low = stay at 10400; Medium = 38400 (0x50); High = 124800 (0x87).
"""
import logging
import time
from dataclasses import dataclass

from .transport import KLineError, KLineTimeout
from .crypto import compute_lvl41_key
from . import kwp as _kwp
from . import loaders as _loaders

FLASH_SIZE = 0x80000
FLASH_END = 0x7FEE2                 # ReadFlash packet loop end (last 0x11E bytes via 2 tail reads)
BOOT_START, BOOT_END = 0x7C000, 0x7FFFF
MAIN_START, MAIN_END = 0x00000, 0x7BFFF
EEPROM_SIZE = 512

SPEEDS = {"Low": None, "Medium": (0x50, 38400), "High": (0x87, 124800)}

# Am29F400BT top-boot sectors (flash-relative), 512 KB part
SECTORS = [(0x00000, 0x10000), (0x10000, 0x20000), (0x20000, 0x30000), (0x30000, 0x40000),
           (0x40000, 0x50000), (0x50000, 0x60000), (0x60000, 0x70000),
           (0x70000, 0x78000), (0x78000, 0x7A000), (0x7A000, 0x7C000), (0x7C000, 0x80000)]
SECTOR_WRITE_BASE = 0x200000        # CPU-bus address the sector loader erases/programs at
SECTOR_READ_BASE = 0x080000         # ...and the 512 KB whole-chip read loop reads/checksums at

# Am29F800BT top-boot sectors (flash-relative), 1 MB part: fifteen 64 KB
# sectors then the 32/8/8/16 KB top-boot group, mirroring the 29F400BT layout
# one segment higher. The KWP2000 boot stub lives in the top 16 KB (0xFC000).
SECTORS_1M = ([(i << 16, (i + 1) << 16) for i in range(15)]
              + [(0xF0000, 0xF8000), (0xF8000, 0xFA000), (0xFA000, 0xFC000), (0xFC000, 0x100000)])


@dataclass(frozen=True)
class FlashLayout:
    """Everything that differs between the 512 KB and 1 MB EDC15 flash parts.

    Bench-proven on a 512 KB ECU: the flash answers reads at every external CPU
    address except the 0x200000-0x3FFFFF block, which is the write-only
    programming window (reads return an open-bus pattern). The chip simply sees
    the CPU address lines, so ``chip_byte = cpu & (size - 1)``: a 512 KB part
    ignores A19, a 1 MB part decodes it. Hence

    * 512 KB: ``read_base`` 0x080000 (historical, bench-proven) -- the whole chip.
    * 1 MB: the lower half (chip 0x00000-0x7FFFF, A19 = 0) is read through
      ``read_base`` 0x100000 and the upper half (A19 = 1) through the window the
      firmware itself uses, 0x080000 (``split_base``), so the upper half takes
      the proven path and only the lower half depends on an inferred window.

    ``cpu_read(off)`` gives the CPU address to read chip offset ``off`` at.
    ``write_base`` is the programming window for erase/program (both sizes): the
    1 MB lower half programs at 0x200000-0x27FFFF, the upper at 0x280000-0x2FFFFF.
    The 1 MB windows are inferred from the aliasing and not yet bench-proven on a
    1 MB ECU. See docs/edc15-1mb.md.
    """
    name: str
    size: int
    sectors: tuple
    boot_start: int
    boot_end: int
    read_base: int
    write_base: int = SECTOR_WRITE_BASE
    split: int = 0              # chip offset where a second read window starts (0: none)
    split_base: int = 0         # CPU address that chip offset ``split`` is read at

    def cpu_read(self, off: int) -> int:
        """CPU address to read chip offset ``off`` at."""
        if self.split and off >= self.split:
            return self.split_base + (off - self.split)
        return self.read_base + off


LAYOUT_512 = FlashLayout("512 KB (Am29F400BT)", FLASH_SIZE, tuple(SECTORS),
                         BOOT_START, BOOT_END, SECTOR_READ_BASE)
LAYOUT_1M = FlashLayout("1 MB (Am29F800BT)", 0x100000, tuple(SECTORS_1M),
                        0xFC000, 0xFFFFF, 0x100000, split=0x80000,
                        split_base=SECTOR_READ_BASE)

#: Span the A19-decode size probe compares (small; decisive and quick).
PROBE_LEN = 0x1000

#: CPU addresses the window survey samples, 0x70000 into each 512 KB window:
#: A19=0 windows (lower half of a 1 MB part), A19=1 windows (upper half), and the
#: write-only programming block.
SURVEY_ADDRS = (0x070000, 0x0F0000, 0x170000, 0x1F0000, 0x270000, 0x2F0000,
                0x470000, 0x4F0000, 0x870000, 0x8F0000)

#: Read windows used to test CPU A19. All are bench-proven aliases of the same
#: flash cell on a 512 KB ECU. ``A19_HI`` have A19=1, ``A19_LO`` have A19=0; each
#: is 0x70000 into a 512 KB window, clear of the internal ROM and the 0x07F000
#: peripheral.
A19_HI = (0x0F0000, 0x1F0000, 0x8F0000)
A19_LO = (0x170000, 0x470000, 0x870000)

# The C167's own 32 KB internal boot ROM, as the RUNNING ECU maps it. Much of
# the firmware lives here rather than in the flash: each reference image makes
# ~1000 `CALLS 00H, <off>` into it, 99.4-99.8% at offsets <= 0x7FFF. The V4.1
# checksum worker is one of these routines (`CALLS 00H, 1584H` -- sub_1584,
# ported as ovtsuite.flash.loaders.loader_checksum).
#
# Reading it needs NO boot mode and no boot pin: the running firmware has the
# ROM mapped because it is executing out of it, and the sector loader's Cmd23
# takes an arbitrary CPU-bus address, so `read_cpu_range` reaches it with no
# bus or register reconfiguration at all.
IROM_BASE = 0x000000
IROM_SIZE = 0x8000
PROGRAM_CHUNK = 128
READ_CHUNK = 58
LOADER_ADDR = bytes([0x40, 0xE0, 0x00])
KEY3 = 0x3800000

# per-variant: LVL41 key, post-loader handshake request bytes, expected replies
VARIANTS = {
    "P": (0xDA1CF781,
          bytes([0x08, 0x31, 0x02, 0x10, 0x00, 0x00, 0x0F, 0xBF, 0xFF,
                 0x05, 0x23, 0x08, 0x00, 0x00, 0x10,
                 0x05, 0x23, 0x10, 0x00, 0x00, 0x10,
                 0x08, 0x31, 0x02, 0x08, 0x00, 0x00, 0x0F, 0xBF, 0xFF]),
          bytes([0x03, 0x7F, 0x31, 0x22,
                 0x11, 0x36, 0x0A, 0x01, 0x04, 0x00, 0x05, 0x00, 0x0B, 0x00, 0x0C, 0x00,
                 0x0D, 0x00, 0x0F, 0x00, 0x10, 0x00,
                 0x11, 0x36, 0x0A, 0x01, 0x04, 0x00, 0x05, 0x00, 0x0B, 0x00, 0x0C, 0x00,
                 0x0D, 0x00, 0x0F, 0x00, 0x10, 0x00,
                 0x01, 0x7F])),
    "V": (0x508DA647,
          bytes([0x08, 0x31, 0x02, 0x08, 0x00, 0x00, 0x0F, 0xBF, 0xFF]),
          bytes([0x03, 0x7F, 0x31, 0x22])),
    "VM": (0xF25E6533,
           bytes([0x08, 0x31, 0x02, 0x10, 0x00, 0x00, 0x0F, 0xBF, 0xFF,
                  0x05, 0x23, 0x08, 0x00, 0x00, 0x10,
                  0x05, 0x23, 0x10, 0x00, 0x00, 0x10,
                  0x08, 0x31, 0x02, 0x08, 0x00, 0x00, 0x0F, 0xBF, 0xFF]),
           bytes([0x03, 0x7F, 0x31, 0x22,
                  0x11, 0x36, 0xDB, 0x00, 0xF2, 0xF0, 0x40, 0x00, 0x26, 0xF0, 0x08, 0x00,
                  0x84, 0x00, 0x72, 0xFB, 0xF6, 0xF0,
                  0x11, 0x36, 0xDB, 0x00, 0xF2, 0xF0, 0x40, 0x00, 0x26, 0xF0, 0x08, 0x00,
                  0x84, 0x00, 0x72, 0xFB, 0xF6, 0xF0,
                  0x01, 0x7F])),
}
AUTO_ORDER = ("V", "P", "VM")
#: Wait between loader TransferData frames, on top of the KWP2000 P3.
TRANSFER_GAP = 0.005

_debug = logging.getLogger(__name__).debug


def _cks(data) -> int:
    return sum(data) & 0xFF


def sector_checksum(data) -> int:
    """Client twin of the sector loader's CmdB2: 16-bit rotate-left-1-then-add."""
    acc = 0
    for b in data:
        acc = ((acc << 1) | (acc >> 15)) & 0xFFFF
        acc = (acc + b) & 0xFFFF
    return acc


def _plausible_flash(data) -> bool:
    """Real flash/code has many distinct byte values; an unmapped bus returns
    one or two (e.g. the high address byte and 0x00, repeated)."""
    return len(set(bytes(data))) >= 8


def eeprom_info(ee) -> dict:
    """Decode the well-known EDC15 EEPROM fields (KWHack Edc15VM.GetEepromInfo)."""
    ee = bytes(ee)
    skc = ee[0x12E] | (ee[0x12F] << 8)
    odo = (ee[0x1BF] + (ee[0x1C0] << 8) + (ee[0x1C1] << 16)
           + ((ee[0x1C2] & 0x3F) << 24)) / 100.0
    vin = "".join(chr(b) for b in ee[0x140:0x151])
    immo_no = "".join(chr(b) for b in ee[0x131:0x13F])
    immo_id = ee[0x126:0x12D].hex(" ").upper()
    immo_on = not (ee[0x1B0] == 0x60 and ee[0x1DE] == 0x60)
    return {"skc": skc, "odometer_km": odo, "vin": vin, "immo_number": immo_no,
            "immo_id": immo_id, "immo_on": immo_on}


class StopRequested(Exception):
    pass


class SoftBrick(KLineError):
    """The 9600 KW1281 wakeup answered with a KWP2000 keyword: the ECU is stuck
    in its flash boot stub (recovery mode)."""


class Edc15Flash:
    def __init__(self, link, log=None):
        self.k = link
        self._log = log or (lambda s: None)
        self._fast = False
        self._loader = None             # None | "flash" | "sector" | "eeprom"
        self._loader_started = False
        self.kwp = None
        self.variant = None
        self.recovered = False
        #: flash part geometry; stays 512 KB until :meth:`detect_layout` says
        #: otherwise, so nothing changes for a 512 KB ECU.
        self.layout = LAYOUT_512
        #: shorter session timing the link asks for (the J2534 byte-stream
        #: link's ``edc15_timing``, switched by kline_j2534_raw's
        #: EDC15_J2534_FAST_TIMING); empty: the timing used on a serial cable
        get = getattr(link, "edc15_timing", None)
        self._timing = (get() if get is not None else None) or {}
        if get is not None:
            _debug("EDC15 over J2534: %s session timing %s",
                   "fast" if self._timing else "previous", self._timing or "")

    def log(self, msg):
        self._log(msg)

    @staticmethod
    def _stop(is_stop):
        if is_stop and is_stop():
            raise StopRequested()

    # ------------------------------------------------------------ connect
    def connect(self, variant="Auto", speed="Medium", loader="flash", is_stop=None):
        """Wake the ECU, authenticate, negotiate speed and upload ``loader``
        ("flash" whole-chip, "sector" per-sector, "eeprom")."""
        if variant == "Auto":
            last = None
            for i, v in enumerate(AUTO_ORDER):
                self._stop(is_stop)
                try:
                    self._connect_core(v, speed, loader, is_stop)
                    self.log("Auto-detected EDC15 variant: %s" % v)
                    return
                except StopRequested:
                    raise
                except Exception as ex:             # noqa: BLE001 - try next variant
                    last = ex
                    if i < len(AUTO_ORDER) - 1:
                        self.log("Variant %s didn't work (%s); trying next..." % (v, ex))
                        if self._loader_started:
                            self.try_close()
            raise last
        self._connect_core(variant, speed, loader, is_stop)

    def _connect_core(self, variant, speed, loader, is_stop):
        self.variant = variant
        attempts = 3
        for attempt in range(1, attempts + 1):
            self._stop(is_stop)
            try:
                self._connect_once(variant, speed, loader, is_stop)
                return
            except SoftBrick:
                self.log("ECU is soft-bricked (KWP2000 boot stub) -- switching to recovery mode...")
                self.recovered = True
                self._connect_once(variant, "Low", loader, is_stop,
                                   require_kw1281=False, skip_0x89=True)
                return
            except StopRequested:
                raise
            except Exception as ex:                 # noqa: BLE001
                if attempt < attempts and not self._loader_started:
                    self.log("Connect attempt %d/%d failed before the loader started (%s); "
                             "retrying with a fresh wakeup..." % (attempt, attempts, ex))
                    time.sleep(0.5)
                    continue
                raise

    def _connect_once(self, variant, speed, loader, is_stop,
                      require_kw1281=True, skip_0x89=False):
        self._loader_started = False
        self._fast = False
        self._loader = None
        info = VARIANTS[variant]

        if require_kw1281:
            self.k.set_baud(9600)
            self.log("Connecting to EDC15 (slow init, KW1281)...")
            kw1281 = _kwp.Kw1281(self.k, self.log,
                                 fast=bool(self._timing.get("kw1281_fast")))
            primary_failure = None
            try:
                version = self.k.wake_up(0x01, even_parity=False)
                if version != 1281:
                    raise SoftBrick("KW1281 wakeup answered with KW %d" % version)
            except SoftBrick:
                raise
            except KLineError as ex:
                primary_failure = ex
            if primary_failure is not None:
                sync = getattr(self.k, "last_sync", -1)
                if sync not in (0x95, 0xB5):
                    raise primary_failure
                self.log("Soft-brick sync byte 0x%02X at 9600; probing at 10400..." % sync)
                self.k.set_baud(10400)
                try:
                    probe = self.k.wake_up(0x01, even_parity=False)
                except KLineError:
                    raise primary_failure
                if probe < 2000:
                    raise primary_failure
                self.log("ECU answered in KWP2000 mode (KW %d) -- soft-bricked; "
                         "continuing in recovery mode." % probe)
                self.recovered = True
                self._continue_kwp(info, "Low", True, loader, is_stop)
                return
            kw1281.connect()
            kw1281.end_communication()
            time.sleep(1.0)
        else:
            self.log("Recovery mode: skipping KW1281, KWP2000 wakeup only.")

        self.k.set_baud(10400)
        version = self.k.wake_up(0x01, even_parity=False)
        if version < 2000:
            raise KLineError("Unable to wake up EDC15 in KWP2000 mode (KW %d)" % version)
        self.log("KWP2000 session (KW %d)." % version)
        self._continue_kwp(info, speed, skip_0x89, loader, is_stop)

    def _continue_kwp(self, info, speed, skip_0x89, loader, is_stop):
        self.k.clear_rx()
        kwp = self.kwp = _kwp.Kwp2000(self.k, 0x01, self.log)
        if self._timing.get("kwp_batched"):
            kwp.fast = True             # whole frame in one write; P3 kept
        if skip_0x89:
            kwp.send_receive(_kwp.SVC_START_DIAG, b"\x85")
        else:
            kwp.send_receive(_kwp.SVC_START_DIAG, b"\x89")
            kwp.send_receive(_kwp.SVC_START_DIAG, b"\x85")
        self.log("Requesting security access...")
        self._seed_auth(kwp, info[0])
        self._try_set_speed(kwp, speed)
        self._stop(is_stop)
        self.log("Uploading %s loader..." % loader)
        self._loader_started = True
        self._loader = loader
        if loader == "flash":
            self._send_flash_loader(kwp, info)
        elif loader == "sector":
            self._upload_loader(kwp, _loaders.sector_loader())
        elif loader == "eeprom":
            self._upload_loader(kwp, _loaders.eeprom_loader())
        else:
            raise ValueError("unknown loader %r" % loader)
        self.log("Loader running.")

    @staticmethod
    def _seed_auth(kwp, key):
        mode = 0x41
        resp = kwp.send_receive(_kwp.SVC_SECURITY, bytes([mode]))
        msg = bytearray([mode + 1])
        if bytes(resp.body) != bytes([mode, 0x00, 0x00]):
            seed = bytes(resp.body[1:5])
            msg += compute_lvl41_key(key, KEY3, seed)
        kwp.send_receive(_kwp.SVC_SECURITY, bytes(msg))

    def _try_set_speed(self, kwp, speed):
        sel = SPEEDS.get(speed)
        if sel is None:
            return
        speed_byte, baud = sel
        try:
            time.sleep(0.075)
            kwp.send_receive(_kwp.SVC_START_DIAG, bytes([0x85, speed_byte]))
            time.sleep(0.075)
            self.k.set_baud(baud)
            self._fast = True
            self.log("ECU agreed to %d baud." % baud)
        except (KLineError, KLineTimeout) as ex:
            self.log("ECU declined %d baud (%s); staying at 10400." % (baud, ex))

    # ---- loader upload ----
    def _upload_loader(self, kwp, data):
        n = len(data)
        resp = kwp.send_receive(
            _kwp.SVC_REQ_DOWNLOAD,
            LOADER_ADDR + bytes([0x00, (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF]),
            exclude_addresses=True)
        max_block = resp.body[0] if resp.body else 0xFF
        chunk = max(1, max_block - 1)
        gap = self._transfer_gap()
        for off in range(0, n, chunk):
            if gap:
                time.sleep(gap)
            self._transfer_with_retry(kwp, data[off:off + chunk])
        kwp.send(_kwp.SVC_START_ROUTINE_LID, b"\x02", exclude_addresses=True,
                 _then_read=self._reply_with_write())
        kwp.receive()                                   # NAK 31/22 expected; ignored

    @staticmethod
    def _transfer_with_retry(kwp, data, attempts=3):
        for attempt in range(1, attempts + 1):
            try:
                kwp.send_receive(_kwp.SVC_TRANSFER, data, exclude_addresses=True)
                return
            except KLineTimeout:
                if attempt >= attempts:
                    raise
                time.sleep(0.2)

    def _send_flash_loader(self, kwp, info):
        time.sleep(0.025)
        resp = kwp.send_receive(_kwp.SVC_REQ_DOWNLOAD,
                                LOADER_ADDR + bytes([0x00, 0x00, 0x04, 0x20]),
                                exclude_addresses=True)
        gap = self._transfer_gap()
        if gap:
            time.sleep(gap)
        loader = _loaders.flash_loader()
        max_block = resp.body[0] if resp.body else 0xFF
        chunk = max(1, max_block - 1)
        for off in range(0, len(loader), chunk):
            kwp.send_receive(_kwp.SVC_TRANSFER, loader[off:off + chunk], exclude_addresses=True)
            if gap:
                time.sleep(gap)
        kwp.send_receive(_kwp.SVC_TRANSFER,
                         bytes([0xC3, 0xC3, 0x89, 0xC5, 0x45, 0xDA, 0x63, 0x0B]),
                         exclude_addresses=True)
        if gap:
            time.sleep(gap)
        _, ardu, ecu = info
        a = e = 0
        if len(ardu) == 9:
            a = self._send_slice(ardu, a, 9); e = self._verify_slice(ecu, e, 4)
        else:
            a = self._send_slice(ardu, a, 9); e = self._verify_slice(ecu, e, 4)
            a = self._send_slice(ardu, a, 6); e = self._verify_slice(ecu, e, 18)
            a = self._send_slice(ardu, a, 6); e = self._verify_slice(ecu, e, 18)
            a = self._send_slice(ardu, a, 9); e = self._verify_slice(ecu, e, 2)

    def _transfer_gap(self):
        return self._timing.get("transfer_gap", TRANSFER_GAP)

    def _reply_with_write(self):
        return bool(self._timing.get("loader_write_with_read"))

    def _write_for_reply(self, data):
        """Write a loader command whose reply is read next: with the read, in
        one device call, where the session timing says so and the link can."""
        w = getattr(self.k, "write_before_read", None) if self._reply_with_write() else None
        (w or self.k.write_bytes)(bytes(data))

    def _write_delayed(self, b):
        self.k.write_byte(b)
        if not self._fast:
            time.sleep(0.005)

    def _send_slice(self, src, pos, n):
        part = src[pos:pos + n]
        if self._timing.get("handshake_batched"):
            self._write_for_reply(bytes(part) + bytes([_cks(part)]))
            return pos + n
        for b in part:
            self._write_delayed(b)
        self._write_delayed(_cks(part))
        return pos + n

    def _verify_slice(self, expected, pos, n):
        got = bytearray(self.k.read_byte() for _ in range(n))
        cksb = self.k.read_byte()
        want = expected[pos:pos + n]
        if bytes(got) != bytes(want):
            raise KLineError("EDC15 handshake mismatch: expected %s got %s"
                             % (want.hex(), got.hex()))
        if cksb != _cks(got):
            raise KLineError("EDC15 handshake checksum mismatch")
        return pos + n

    # ------------------------------------------------ flash-loader primitives
    def _check_rec(self, expected):
        b = self.k.read_byte()
        if b == 0x00 and expected != 0x00:              # spurious leading zero
            b = self.k.read_byte()
        if b != expected:
            raise KLineError("EDC15: expected 0x%02X, got 0x%02X" % (expected, b))

    def _check_ack(self):
        self._check_rec(0x01)
        self._check_rec(0x76)
        self._check_rec(0x77)

    def _read_flash_packet(self):
        while True:
            length = self.k.read_byte()
            service = self.k.read_byte()
            if service == 0x00:
                service = self.k.read_byte()
            if service == 0x7F:
                n = max(0, length - 1)
                body = self.k.read(n) if n else b""
                self.k.read_byte()
                if n >= 2 and body[1] == 0x78:
                    continue                            # response pending
                raise KLineError("EDC15 flash read: NAK %s" % body.hex())
            if service != 0x36:
                raise KLineError("EDC15 flash read: expected 0x36, got 0x%02X" % service)
            n = length - 1
            data = self.k.read(n) if n > 0 else b""
            cksb = self.k.read_byte()
            if cksb != (length + service + sum(data)) & 0xFF:
                raise KLineError("EDC15 flash read: packet checksum mismatch")
            return data

    def _read_packet_with_retry(self, req, attempts=10):
        for attempt in range(1, attempts + 1):
            self._write_for_reply(req)
            try:
                return self._read_flash_packet()
            except (KLineError, KLineTimeout):
                if attempt >= attempts:
                    raise
                self.k.clear_rx()

    def _flash_read_cpu(self, cpu, n) -> bytes:
        """One whole-chip-loader read of ``n`` (<=0xFE) bytes at CPU ``cpu``."""
        req = bytearray([0x05, 0x23, (cpu >> 16) & 0xFF, (cpu >> 8) & 0xFF, cpu & 0xFF, n])
        req.append(_cks(req))
        return self._read_packet_with_retry(bytes(req))

    def read_flash(self, on_percent=None, on_stage=None, is_stop=None) -> bytes:
        """Read the whole flash via the whole-chip loader (call
        ``connect(loader="flash")`` first). Size/window come from
        :attr:`layout`; for a 512 KB part this is byte-identical to before --
        the chunking lands the same 0xFE packets plus the trailing 0x20 read
        because 0x80000 = 2064*0xFE + 0x20."""
        if self._loader != "flash":
            raise KLineError("read_flash needs the flash loader (connect(loader='flash'))")
        lay = self.layout
        stage = on_stage or (lambda s: None)
        stage("Reading flash (%s)..." % lay.name)
        out = bytearray()
        before = -1
        try:
            pos = 0
            while pos < lay.size:
                self._stop(is_stop)
                n = min(0xFE, lay.size - pos)
                if lay.split and pos < lay.split:
                    n = min(n, lay.split - pos)        # never straddle the two windows
                out += self._flash_read_cpu(lay.cpu_read(pos), n)
                pos += n
                pct = min(100, pos * 100 // lay.size)
                if pct != before and on_percent:
                    on_percent(pct)
                    before = pct
        finally:
            self.try_close()
        if len(out) != lay.size:
            raise KLineError("EDC15 read returned %d bytes, expected %d" % (len(out), lay.size))
        if len(set(out)) < 64:
            raise KLineError(
                "EDC15 read returned no flash data (only %d distinct byte values): "
                "the window read is unmapped. The image was NOT produced." % len(set(out)))
        if on_percent:
            on_percent(100)
        return bytes(out)

    def _send_write_chunk(self, header, data, attempts=10):
        chunk = bytes(header) + bytes(data) + bytes([(_cks(header) + _cks(data)) & 0xFF])
        for attempt in range(1, attempts + 1):
            try:
                self._write_for_reply(chunk)
                self._check_ack()
                return
            except (KLineError, KLineTimeout):
                if attempt >= attempts:
                    raise
                self.k.clear_rx()

    def _write_block(self, image, start, end, progress, is_stop, inter_chunk=0.0):
        pos = start
        while pos <= end:
            self._stop(is_stop)
            n = min(0xFA, end - pos + 1)
            data = bytes(image[pos:pos + n])
            if any(b != 0xFF for b in data):
                header = bytes([n + 4, 0x36, ((pos >> 16) & 0xFF) + 0x20,
                                (pos >> 8) & 0xFF, pos & 0xFF])
                self._send_write_chunk(header, data)
                if inter_chunk:
                    time.sleep(inter_chunk)
            pos += n
            progress(n)

    def write_flash_full(self, image, on_percent=None, on_stage=None, is_stop=None):
        """Whole-chip erase + program (boot block first, then main). Needs
        ``connect(loader="flash")``."""
        if self._loader != "flash":
            raise KLineError("write_flash_full needs the flash loader")
        lay = self.layout
        if len(image) < lay.size:
            raise ValueError("EDC15 image must be 0x%X bytes; got %d" % (lay.size, len(image)))
        stage = on_stage or (lambda s: None)
        total = lay.size
        done = [0]
        before = [-1]

        def progress(n):
            done[0] += n
            pct = min(100, done[0] * 100 // total)
            if pct != before[0] and on_percent:
                on_percent(pct)
                before[0] = pct

        try:
            stage("Erasing flash (whole chip)...")
            self._write_for_reply(bytes([0x02, 0xA5, 0x0F, 0xB6]))
            self._check_ack()
            time.sleep(4.0)
            self._check_ack()
            stage("Writing boot block...")
            self._write_block(image, lay.boot_start, lay.boot_end, progress, is_stop,
                              inter_chunk=0.02 if self._fast else 0.0)
            stage("Writing main block...")
            self._write_block(image, 0x00000, lay.boot_start - 1, progress, is_stop)
        finally:
            self.try_close()
        stage("Write complete.")
        if on_percent:
            on_percent(100)

    # ----------------------------------------------- sector/eeprom loader I/O
    def _loader_send(self, service, data=b""):
        data = bytes(data)
        if len(data) > 253:
            raise ValueError("loader message too long")
        buf = bytearray([1 + len(data), service]) + data
        buf.append(_cks(buf))
        self._write_for_reply(bytes(buf))

    def _loader_receive(self):
        length = self.k.read_byte()
        service = self.k.read_byte()
        n = length - 1
        data = self.k.read(n) if n > 0 else b""
        cksb = self.k.read_byte()
        if cksb != (length + service + sum(data)) & 0xFF:
            raise KLineError("loader response checksum mismatch")
        return service, data

    def _loader_cmd(self, service, data, expect, what, attempts=10):
        for attempt in range(1, attempts + 1):
            self._loader_send(service, data)
            try:
                svc, body = self._loader_receive()
                if svc != expect:
                    raise KLineError("%s failed: response 0x%02X (expected 0x%02X)"
                                     % (what, svc, expect))
                return body
            except (KLineError, KLineTimeout):
                if attempt >= attempts:
                    raise
                self.k.clear_rx()
        raise KLineTimeout("no valid response to %s" % what)

    def _checksum_cpu(self, cs, ce):
        """The sector loader's CmdB2 over an absolute CPU-bus range [cs, ce):
        the ECU reads and hashes the span itself and returns only the 16-bit
        result, so the span's size costs nothing on the K-line."""
        body = bytes([(cs >> 16) & 0xFF, (cs >> 8) & 0xFF, cs & 0xFF,
                      (ce >> 16) & 0xFF, (ce >> 8) & 0xFF, ce & 0xFF])
        data = self._loader_cmd(0xB2, body, 0xB2, "checksum 0x%06X-0x%06X" % (cs, ce))
        if len(data) < 2:
            raise KLineError("checksum query returned %d byte(s)" % len(data))
        return (data[0] << 8) | data[1]

    def _sector_checksum_ecu(self, start, end):
        cs = self.layout.cpu_read(start)
        return self._checksum_cpu(cs, cs + (end - start))

    def _erase_sector(self, cpu_addr):
        self._loader_cmd(0xB0, bytes([(cpu_addr >> 16) & 0xFF, (cpu_addr >> 8) & 0xFF,
                                      cpu_addr & 0xFF]), 0x76, "erase sector 0x%X" % cpu_addr)
        saved = self.k.read_timeout
        try:
            self.k.read_timeout = 20.0                  # second ACK arrives when erase is done
            svc, _ = self._loader_receive()
        finally:
            self.k.read_timeout = saved
        if svc != 0x76:
            raise KLineError("erase sector 0x%X: post-erase ACK 0x%02X" % (cpu_addr, svc))

    def _program_sector(self, cpu_addr, data, progress, is_stop):
        if len(data) == 0 or len(data) % 2:
            raise ValueError("sector data must be even-length")
        for off in range(0, len(data), PROGRAM_CHUNK):
            self._stop(is_stop)
            chunk = bytes(data[off:off + PROGRAM_CHUNK])
            if any(b != 0xFF for b in chunk):
                a = cpu_addr + off
                self._loader_cmd(0x36, bytes([(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF]) + chunk,
                                 0x76, "program 0x%X" % a)
            progress(len(chunk))

    def read_cpu_range(self, cpu_addr, length, on_percent=None, is_stop=None):
        """Read ``length`` bytes from an arbitrary CPU-bus address.

        The sector loader's Cmd23 takes a full 24-bit CPU-bus address, so this
        reaches anything the running ECU has mapped -- the external flash at
        :data:`SECTOR_READ_BASE`, and the C167 internal ROM at
        :data:`IROM_BASE`. Nothing is reconfigured: the addresses are whatever
        the firmware itself is already using.
        """
        if self._loader != "sector":
            raise KLineError('Cmd23 reads need the sector loader '
                             '(connect(loader="sector"))')
        out = bytearray()
        off = 0
        before = -1
        while off < length:
            self._stop(is_stop)
            n = min(READ_CHUNK, length - off)
            a = cpu_addr + off
            data = self._loader_cmd(
                0x23, bytes([(a >> 16) & 0xFF, (a >> 8) & 0xFF, a & 0xFF, n]),
                0x36, "read CPU 0x%06X" % a)
            if len(data) != n:
                raise KLineError("read CPU 0x%06X: got %d of %d bytes" % (a, len(data), n))
            out += data
            off += n
            if on_percent:
                pct = off * 100 // max(length, 1)
                if pct != before:
                    on_percent(pct)
                    before = pct
        return bytes(out)

    def read_flash_range(self, flash_addr, length):
        """Read a flash range, which the running ECU maps at
        :data:`SECTOR_READ_BASE` (the old ``+0x08`` address-high convention,
        which is just that base expressed as a carry into the high byte)."""
        return self.read_cpu_range(SECTOR_READ_BASE + flash_addr, length)

    # -------------------------------------------------- 512 KB vs 1 MB detect
    def detect_layout(self, is_stop=None):
        """Pick :attr:`layout` (512 KB vs 1 MB) by testing whether CPU A19
        reaches the flash, with no content signature.

        The flash answers reads in many aliased windows (see :class:`FlashLayout`).
        Windows that differ only in A19 show the *same* cell on a 512 KB part
        (it ignores A19) and the two *halves* on a 1 MB part. So:

        1. the A19=1 windows must read identically (proves they alias the proven
           read window) and look like flash;
        2. the A19=0 windows must read identically to each other;
        3. A19=0 equal to A19=1 means 512 KB, different means 1 MB.

        Any step that fails (no flash-like data, inconsistent aliases) is
        inconclusive and keeps 512 KB, the bench-proven behaviour. A 1 MB part on
        which A19 never reaches the chip is therefore also taken as 512 KB.

        The sector loader compares on the ECU with its checksum command (only
        16-bit results cross the wire); the flash loader reads short spans.
        """
        if self._loader not in ("sector", "flash"):
            raise KLineError("size detection needs the sector or flash loader")
        self.layout = LAYOUT_512
        hi, lo = A19_HI, A19_LO
        sample = self._probe_sample(hi[0])
        self.log("Size probe: A19=1 window 0x%06X -> %s" % (hi[0], sample[:8].hex(" ")))
        if not _plausible_flash(sample):
            return self._inconclusive("the read window returned no flash data", hi[0], sample)
        for name, group in (("A19=1", hi), ("A19=0", lo)):
            for other in group[1:]:
                self._stop(is_stop)
                if not self._probe_equal(group[0], other):
                    return self._inconclusive(
                        "%s windows 0x%06X and 0x%06X disagree" % (name, group[0], other),
                        hi[0], sample)
        self._stop(is_stop)
        same = self._probe_equal(lo[0], hi[0])
        self.layout = LAYOUT_512 if same else LAYOUT_1M
        self.log("Size probe: A19=0 window 0x%06X -> %s (%s the A19=1 window)"
                 % (lo[0], self._probe_sample(lo[0])[:8].hex(" "), "same as" if same else "differs from"))
        self.log("Detected EDC15 flash: %s." % self.layout.name)
        if not same:
            self._survey_windows(hi[0], sample)
        return self.layout

    def _inconclusive(self, why, known, sample):
        self.log("Size probe inconclusive (%s); assuming a 512 KB flash." % why)
        self._survey_windows(known, sample)
        return self.layout

    def _survey_windows(self, known, ref):
        """Log what each candidate CPU window returns (read-only), so a bench
        log shows which windows are flash, which half of a 1 MB part they
        reach, and which are open bus. ``ref`` is the sample read at ``known``
        through the proven window."""
        self.log("Window survey (reference 0x%06X = %s):" % (known, ref[:8].hex(" ")))
        for cpu in SURVEY_ADDRS:
            try:
                got = self._probe_sample(cpu)
            except (KLineError, KLineTimeout) as ex:
                self.log("  0x%06X: no reply (%s)" % (cpu, ex))
                continue
            kind = ("same as the read window" if got[:len(ref)] == ref[:len(got)]
                    else "flash-like, differs" if _plausible_flash(got)
                    else "open bus / pattern")
            self.log("  0x%06X: %s  [%s]" % (cpu, got[:8].hex(" "), kind))

    def _probe_sample(self, cpu) -> bytes:
        """A short real read at ``cpu`` (a few dozen bytes) for inspection."""
        if self._loader == "sector":
            return self.read_cpu_range(cpu, READ_CHUNK)
        return self._flash_read_cpu(cpu, 0x40)

    def _probe_equal(self, a, b) -> bool:
        """Do the ``PROBE_LEN`` spans at CPU ``a`` and ``b`` read identical?"""
        if self._loader == "sector":
            return self._checksum_cpu(a, a + PROBE_LEN) == self._checksum_cpu(b, b + PROBE_LEN)
        n = min(0xFE, PROBE_LEN)                 # flash loader: one packet is plenty
        return self._flash_read_cpu(a, n) == self._flash_read_cpu(b, n)

    def read_irom(self, on_percent=None, on_stage=None, is_stop=None) -> bytes:
        """Read the C167 internal boot ROM over K-line -- no boot mode needed.

        Check the result with :func:`ovtsuite.edc15.irom15.check` before
        trusting it: a misconfigured C167 read does not fail, it silently
        returns every other byte doubled, and that check is what catches it.
        """
        stage = on_stage or self.log
        stage("Reading C167 internal ROM (CPU 0x%06X-0x%06X)..."
              % (IROM_BASE, IROM_BASE + IROM_SIZE - 1))
        return self.read_cpu_range(IROM_BASE, IROM_SIZE,
                                   on_percent=on_percent, is_stop=is_stop)

    def write_flash_sectors(self, image, verify=True, force_full=False,
                            on_percent=None, on_stage=None, is_stop=None):
        """Per-sector checksum-driven write (needs ``connect(loader="sector")``).
        Returns the list of sector indexes written."""
        if self._loader != "sector":
            raise KLineError("write_flash_sectors needs the sector loader")
        lay = self.layout
        if len(image) < lay.size:
            raise ValueError("EDC15 image must be 0x%X bytes; got %d" % (lay.size, len(image)))
        stage = on_stage or (lambda s: None)
        try:
            to_write = []
            stage("Full write: every sector will be rewritten." if force_full
                  else "Comparing sector checksums...")
            for i, (s, e) in enumerate(lay.sectors):
                self._stop(is_stop)
                if force_full:
                    to_write.append(i)
                    continue
                ecu = self._sector_checksum_ecu(s, e)
                mine = sector_checksum(image[s:e])
                if ecu == mine:
                    self.log("Sector %d (0x%X-0x%X): unchanged, skipping." % (i, s, e))
                else:
                    self.log("Sector %d (0x%X-0x%X): differs (ECU %04X, file %04X)."
                             % (i, s, e, ecu, mine))
                    to_write.append(i)
            if not to_write:
                stage("All sectors already match the image -- nothing to write.")
                if on_percent:
                    on_percent(100)
                return []

            total = sum(lay.sectors[i][1] - lay.sectors[i][0] for i in to_write)
            done = [0]
            before = [-1]

            def progress(n):
                done[0] += n
                pct = min(100, done[0] * 100 // total)
                if pct != before[0] and on_percent:
                    on_percent(pct)
                    before[0] = pct

            for k, i in enumerate(to_write):
                self._stop(is_stop)
                s, e = lay.sectors[i]
                stage("Writing sector %d of %d (0x%05X-0x%05X)..." % (k + 1, len(to_write), s, e - 1))
                cpu = s + lay.write_base
                self._erase_sector(cpu)
                self._program_sector(cpu, image[s:e], progress, is_stop)

            if verify:
                stage("Verifying written sectors...")
                bad = []
                for i in to_write:
                    s, e = lay.sectors[i]
                    if self._sector_checksum_ecu(s, e) != sector_checksum(image[s:e]):
                        bad.append(i)
                if bad:
                    raise KLineError("Sector(s) %s failed post-write checksum verification"
                                     % ", ".join(str(b) for b in bad))
                stage("Verify passed: all written sectors match the image.")
            return to_write
        finally:
            self.try_close()

    # ------------------------------------------------------------- EEPROM
    def _eeprom_dump(self):
        kwp = self.kwp
        kwp.send(0xA6, b"", exclude_addresses=True, _then_read=self._reply_with_write())
        resp = kwp.receive()
        if not resp.is_positive(_kwp.SVC_TRANSFER):
            raise KLineError("Dump EEPROM failed (0x%02X)" % resp.service)
        ee = self.k.read(EEPROM_SIZE)
        kwp.receive()                                   # trailing ACK
        return bytes(ee)

    def _eeprom_write_pairs(self, pairs):
        kwp = self.kwp
        i = 0
        while i < len(pairs):
            page1 = pairs[i][0] > 0xFF
            j = i + 1
            while j < len(pairs) and (pairs[j][0] > 0xFF) == page1 and j - i < 255:
                j += 1
            run = pairs[i:j]
            kwp.send(0xAA if page1 else 0xA9, bytes([len(run)]), exclude_addresses=True,
                     _then_read=self._reply_with_write())
            if not kwp.receive().is_positive(_kwp.SVC_TRANSFER):
                raise KLineError("Write EEPROM (batched) failed")
            raw = bytearray()
            for addr, val in run:
                raw += bytes([addr & 0xFF, val & 0xFF])
            self._write_for_reply(bytes(raw))
            if not kwp.receive().is_positive(_kwp.SVC_TRANSFER):
                raise KLineError("Write EEPROM (batched) failed")
            i = j

    def read_eeprom(self, on_stage=None) -> bytes:
        """Dump the 512-byte EEPROM (needs ``connect(loader="eeprom")``)."""
        if self._loader != "eeprom":
            raise KLineError("read_eeprom needs the EEPROM loader")
        stage = on_stage or (lambda s: None)
        try:
            stage("Reading EEPROM...")
            ee = self._eeprom_dump()
        finally:
            self.try_close()
        return ee

    def write_eeprom(self, new, on_stage=None, on_percent=None, is_stop=None):
        """Write ``new`` (512 bytes): dump, write only differing bytes, then
        read back in-session and verify. Returns (readback, changed_count)."""
        if self._loader != "eeprom":
            raise KLineError("write_eeprom needs the EEPROM loader")
        new = bytes(new)
        if len(new) != EEPROM_SIZE:
            raise ValueError("EEPROM image must be %d bytes; got %d" % (EEPROM_SIZE, len(new)))
        stage = on_stage or (lambda s: None)
        try:
            stage("Reading current EEPROM...")
            cur = self._eeprom_dump()
            pairs = [(a, new[a]) for a in range(EEPROM_SIZE) if cur[a] != new[a]]
            if on_percent:
                on_percent(30)
            if not pairs:
                stage("EEPROM already matches -- nothing to write.")
                if on_percent:
                    on_percent(100)
                return cur, 0
            self._stop(is_stop)
            stage("Writing %d changed byte(s)..." % len(pairs))
            self._eeprom_write_pairs(pairs)
            if on_percent:
                on_percent(70)
            stage("Reading back to verify...")
            back = self._eeprom_dump()
            if back != new:
                bad = [a for a in range(EEPROM_SIZE) if back[a] != new[a]]
                raise KLineError("EEPROM verify failed at %d byte(s) (first 0x%03X)"
                                 % (len(bad), bad[0]))
            stage("EEPROM written and verified.")
            if on_percent:
                on_percent(100)
            return back, len(pairs)
        finally:
            self.try_close()

    # -------------------------------------------------------------- close
    def try_close(self):
        """Best-effort 'reboot to normal operation' for whichever loader is
        running (all three understand 0xA2: ``01 A2 A3``)."""
        if not self._loader_started:
            return
        self._loader_started = False
        try:
            self.k.write_bytes(bytes([0x01, 0xA2, 0xA3]))
            saved = self.k.read_timeout
            self.k.read_timeout = 1.0
            got = bytearray()
            try:
                while len(got) < 8:
                    got.append(self.k.read_byte())
            except KLineTimeout:
                pass
            finally:
                self.k.read_timeout = saved
            if 0x55 in got:
                self.log("ECU rebooted to normal operation.")
            else:
                self.log("(Loader close: no 0x55 seen; got %s)" % bytes(got).hex())
        except Exception as ex:                          # noqa: BLE001 - best effort
            self.log("(Best-effort loader close failed: %s)" % ex)

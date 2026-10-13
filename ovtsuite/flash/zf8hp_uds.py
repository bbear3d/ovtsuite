# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r"""ZF 8HP (Gen1) TCU flash over OBD / UDS (ISO 15765) -- the flasher core.

STATUS (2026-10-10): **developed against the bench simulator
(ovtsuite.flash.ecu_sim profile "8hp"), NOT yet exercised against a real
TCU.** Everything here that is known is read from the TCU's own flash-delivery
files; everything that is assumed is marked ASSUMED below and must be
confirmed on a car (or by capturing an OEM-class tool with the simulator,
exactly as the 6HP28 sequence was) before the GUI offers a write.

KNOWN from the 19 real 8K0/8R0927158 FRFs (see ovtsuite.frf / ovtsuite.zf8hp):
  * the four download blocks, their UDS block identifiers (the ODX
    ``SOURCE-START-ADDRESS`` 01..04) and their requestDownload sizes (the ODX
    ``UNCOMPRESSED-SIZE``): DB_1 0x13FE00, DB_2 0x1E00, DB_3 0x1FE00, DB_4 0x7FE00;
  * the erase regions (``DB_nERASE``: 0x140000 / 0x2000 / 0x20000 / 0x80000);
  * the dataFormatIdentifier 0x22 and its codec (XOR ``CyA2008ZFVAGtcuxsam`` +
    LZSS -- :func:`ovtsuite.flash.crypto.zf8hp_frf_encode`, OEM-exact);
  * the per-block CRC32F the ODX declares = ``zlib.crc32`` of the plaintext
    block -- the value a VAG ``0x0202`` checksum routine compares against;
  * the SA2 write-security tape, byte-identical to the 6HP28's
    (``ZF6HP_WRITE_TAPE``), so ``27 11 / 27 12`` keys come from
    :func:`ovtsuite.flash.crypto.zf6hp_write_key`;
  * the blocks' absolute bases in the 2 MB flash, from their ZF descriptors.

ASSUMED (the VAG ODX-F programming choreography, verified on the 6HP28 in this
same car family and bench-captured from ODIS, but not yet on an 8HP):
  * UDS on 0x7E1/0x7E9 (the gearbox TCU's physical ids on B8 cars);
  * the service sequence ``10 03`` -> ``31 01 0203`` -> quiet bus -> ``10 02``
    -> ``27 11/12`` -> ``2E F15A`` -> per block ``31 01 FF00 01 <id>``,
    ``34 22 41 <id> <size:4>``, ``36``..., ``37``, ``31 01 0202 01 <id> 00 04
    <crc:4>`` -> ``31 01 FF01`` -> ``11 01``;
  * the READ path = the 6HP28's (Bear, 2026-10-10: the 8HP shares the 6HP28's
    write tape, so it very likely shares its read access too): manufacturer
    session 0x4F, ``27 03`` -> key = seed + 0x11C5A
    (:func:`ovtsuite.flash.crypto.zf6hp_read_key`) -> ``27 04``, then ``23``
    ALFID 0x14 in 0xF0 chunks. Unverified on a real 8HP: a refusal is reported
    with the TCU's NRC, nothing else is tried.

Transport: the 8HP FRFs are ODX-F/UDS containers like the 6HP28's, so the
delivery protocol is UDS over ISO 15765, not KWP2000 over TP2.0. The TP2.0 path
(:mod:`ovtsuite.flash.zf6hp`) is not applicable to this platform.
"""
import zlib
from typing import List, Optional

from . import crypto as _crypto
from . import zf6hp_uds as _u
from .zf6hp_uds import (BlockSpec, BlockPlan, WritePlan, KLineError, SID_SESSION,
                        SID_SECURITY, SID_READ_MEM, nrc_name)
from ..platforms import zf8hp as _zf8hp

IMAGE_SIZE = _zf8hp.FLASH_SIZE          # 2 MB
DOWNLOAD_FORMAT = _crypto.ZF8HP_DFI     # 0x22

#: The four download blocks: UDS block id, name, flash base, requestDownload
#: size, kind -- bases and sizes from the FRF ODX + ZF descriptors.
ZF8HP_BLOCKS: List[BlockSpec] = [
    BlockSpec(0x01, "DB_1", 0x040000, 0x13FE00, "program"),
    BlockSpec(0x02, "DB_2", 0x006000, 0x001E00, "program"),
    BlockSpec(0x03, "DB_3", 0x020000, 0x01FE00, "descriptor"),
    BlockSpec(0x04, "DB_4", 0x180200, 0x07FE00, "calibration"),
]
ZF8HP_CAL = ZF8HP_BLOCKS[-1]

# Calibration-only shapes accepted: exactly the DB_4 block (what frf-extract /
# a .8hp cal container hold), or the whole 0x180000 erase region (status area
# + block), from which the block is sliced.
_CAL_SHAPES = (ZF8HP_CAL.length, 0x80000)


def _is_blank(region: bytes) -> bool:
    return not region or region.count(0xFF) == len(region) or region.count(0x00) == len(region)


def _block_valid(spec: BlockSpec, pt: bytes):
    """(valid, reason): a block is valid when it carries ZF's descriptor and
    that descriptor places it at this very base -- the structural test every
    FRF block passes, and a blank/garbage region fails."""
    if _is_blank(pt):
        return False, "blank"
    h = _zf8hp.parse_block_header(pt)
    if h is None:
        return False, "no ZF block descriptor"
    if h.base != spec.base:
        return False, "descriptor places it at 0x%X, not 0x%X" % (h.base, spec.base)
    return True, ""


def classify_image(image: bytes, log=None, force_cal_only: bool = False) -> WritePlan:
    """Which blocks a flat image contains and may write, structurally (mirrors
    :func:`ovtsuite.flash.zf6hp_uds.classify_image`): a 2 MB image is sliced
    at the block bases; a calibration-shaped image is DB_4 only. The program
    set is written only when DB_1/DB_2/DB_3 are all present and each carries
    its own ZF descriptor at the right base; otherwise calibration only."""
    log = log or (lambda s: None)
    n = len(image)
    blocks: List[BlockPlan] = []
    warnings: List[str] = []

    if n in _CAL_SHAPES:
        pt = bytes(image[-ZF8HP_CAL.length:]) if n == 0x80000 else bytes(image[:ZF8HP_CAL.length])
        valid, reason = _block_valid(ZF8HP_CAL, pt)
        blocks.append(BlockPlan(ZF8HP_CAL, pt, not _is_blank(pt), valid,
                                zlib.crc32(pt) & 0xFFFFFFFF, reason))
        cal_only = True
    elif n >= IMAGE_SIZE:
        for spec in ZF8HP_BLOCKS:
            pt = bytes(image[spec.base:spec.base + spec.length])
            valid, reason = _block_valid(spec, pt)
            blocks.append(BlockPlan(spec, pt, not _is_blank(pt), valid,
                                    zlib.crc32(pt) & 0xFFFFFFFF, reason))
        prog = [b for b in blocks if not b.spec.is_cal]
        full = all(b.valid for b in prog)
        cal_only = not full
        if not full:
            log("Program set not fully valid (%s) -- treating as calibration-only."
                % ", ".join("%s:%s" % (b.spec.name, "ok" if b.valid else (b.reason or "bad"))
                            for b in prog))
    else:
        raise KLineError("zf8hp UDS write: image is %d bytes; need a full %d-byte image, "
                         "the %d-byte DB_4 calibration block or the 0x80000 calibration region"
                         % (n, IMAGE_SIZE, ZF8HP_CAL.length))

    if force_cal_only and not cal_only:
        cal_only = True
        log("Calibration-only requested -- writing DB_4 only; the program set "
            "(DB_1/DB_2/DB_3) in the image is left in place.")

    # Stored-checksum sanity for every candidate (warning only -- the write
    # transfers bytes verbatim; the TCU's own 0x0202 compares the transfer CRC,
    # which is a different thing from the block's resident CRC-32 word).
    # Through the registry: without the ZF 8HP module there is nothing to
    # check with, and the plan says so instead of staying silent.
    from ..modules import registry as _registry
    _c8 = _registry.checksum("zf8hp")
    if _c8 is None:
        warnings.append("block checksums not checked: "
                        + _registry.missing_text("zf8hp", "checksum checking"))
    for b in blocks:
        if not b.present or _c8 is None:
            continue
        try:
            rep = _c8.verify(b.plaintext)
        except Exception:                 # noqa: BLE001 - sanity only
            continue
        for r in rep.blocks:
            if not r.ok:
                warnings.append("%s stored CRC-32 0x%08X != computed 0x%08X -- correct the "
                                "checksum before writing (the write path transfers bytes "
                                "verbatim)" % (r.name, r.stored, r.computed))

    for b in blocks:
        if b.spec.is_cal:
            b.will_write = b.valid
            if not b.valid:
                b.reason = b.reason or "blank"
        else:
            b.will_write = (not cal_only) and b.valid
            if cal_only and not b.reason:
                b.reason = ("excluded (calibration-only requested)" if force_cal_only
                            else "excluded (calibration-only image)")
    return WritePlan(cal_only, blocks, warnings)


class Zf8hpUdsWriter(_u.Zf6hpUdsWriter):
    """The 6HP28 writer's VAG UDS choreography with the 8HP's dfi, codec and
    block map (see the module docstring for what is known vs assumed).
    ``plan()``/``write()`` behave as on the 6HP28: every present/valid block
    is written and verified by the post-download 0x0202 routine."""
    DOWNLOAD_FORMAT = DOWNLOAD_FORMAT
    encode_block = staticmethod(_crypto.zf8hp_frf_encode)
    classify = staticmethod(classify_image)


# ===========================================================================
# ZF 8HP Gen2 (ALX52) WRITE -- method 0xAA (AES-128-CBC + LZSS), two layouts
# (3 MB cal 0x260200, 4 MB cal 0x2E0200). Bench-only, UNVERIFIED on a car.
# ===========================================================================
GEN2_DOWNLOAD_FORMAT = _crypto.ZF8HP_GEN2_DFI    # 0xAA

#: block tables by layout; the DB_5 kk=0xA1 coding block has no fixed flash
#: base and is not written here (a calibration or program reflash does not
#: touch it). Bases/sizes from the ALX52 ODX + ZF descriptors.
GEN2_BLOCKS_3MB: List[BlockSpec] = [
    BlockSpec(0x01, "DB_1", 0x050200, 0x20FE00, "program"),
    BlockSpec(0x02, "DB_2", 0x006200, 0x001E00, "program"),
    BlockSpec(0x03, "DB_3", 0x020200, 0x02FE00, "descriptor"),
    BlockSpec(0x04, "DB_4", 0x260200, 0x09FE00, "calibration"),
]
GEN2_BLOCKS_4MB: List[BlockSpec] = [
    BlockSpec(0x01, "DB_1", 0x050200, 0x28FE00, "program"),
    BlockSpec(0x02, "DB_2", 0x006200, 0x001E00, "program"),
    BlockSpec(0x03, "DB_3", 0x020200, 0x02FE00, "descriptor"),
    BlockSpec(0x04, "DB_4", 0x2E0200, 0x11FE00, "calibration"),
]
_GEN2_BY_CAL = {0x260200: GEN2_BLOCKS_3MB, 0x2E0200: GEN2_BLOCKS_4MB}


def classify_image_gen2(image: bytes, log=None, force_cal_only: bool = False) -> WritePlan:
    """Gen2 classify: pick the block table by the layout the image presents
    (3 MB cal 0x260200 / 4 MB cal 0x2E0200, or the matching loose DB_4), then
    apply the same present/valid/program-set logic as the Gen1 classify."""
    log = log or (lambda s: None)
    n = len(image)
    # loose DB_4 calibration block (what a .8hp cal / frf-extract holds)
    for blocks in (GEN2_BLOCKS_3MB, GEN2_BLOCKS_4MB):
        cal = blocks[-1]
        if n == cal.length:
            valid, reason = _block_valid(cal, bytes(image))
            bp = BlockPlan(cal, bytes(image), not _is_blank(image), valid,
                           zlib.crc32(bytes(image)) & 0xFFFFFFFF, reason)
            bp.will_write = valid
            return WritePlan(True, [bp], [])
    # full image: choose the table whose calibration descriptor is present
    blocks = None
    for cb, tbl in _GEN2_BY_CAL.items():
        if n >= cb + tbl[-1].length and image[cb:cb + 3] == _zf8hp.BLOCK_MAGIC:
            blocks = tbl; break
    if blocks is None:
        raise KLineError("zf8hp Gen2 UDS write: image is %d bytes with no calibration "
                         "descriptor at 0x260200 or 0x2E0200" % n)
    plans: List[BlockPlan] = []
    for spec in blocks:
        pt = bytes(image[spec.base:spec.base + spec.length])
        valid, reason = _block_valid(spec, pt)
        plans.append(BlockPlan(spec, pt, not _is_blank(pt), valid,
                               zlib.crc32(pt) & 0xFFFFFFFF, reason))
    prog = [b for b in plans if not b.spec.is_cal]
    cal_only = force_cal_only or not all(b.valid for b in prog)
    for b in plans:
        if b.spec.is_cal:
            b.will_write = b.valid
        else:
            b.will_write = (not cal_only) and b.valid
            if cal_only and not b.reason:
                b.reason = "excluded (calibration-only)"
    return WritePlan(cal_only, plans, [])


class Zf8hpGen2UdsWriter(Zf8hpUdsWriter):
    """ZF 8HP Gen2 (ALX52) write: the Gen1 8HP choreography with dfi 0xAA, the
    AES+LZSS encoder, the Gen2 block tables and the Gen2 SA2 write key (run
    from the FRF's own tape). Bench-testable against the ecu_sim "8hp-gen2"
    profile; UNVERIFIED on a real car."""
    DOWNLOAD_FORMAT = GEN2_DOWNLOAD_FORMAT
    encode_block = staticmethod(_crypto.zf8hp_gen2_frf_encode)
    classify = staticmethod(classify_image_gen2)
    write_key = staticmethod(_crypto.zf8hp_gen2_write_key)


class Zf8hpUdsReader(_u.Zf6hpUdsReader):
    """Calibration read over UDS with the 6HP28's read access -- UNVERIFIED on
    a real 8HP. Reuses the 6HP28 reader's link bring-up and chunked ``0x23``
    loop: session 0x4F, read level ``27 03/04`` with key = seed + 0x11C5A
    (zf6hp_read_key), ALFID 0x14, 0xF0-byte chunks. A zero seed means the
    level is already open (no key sent). Defaults read exactly the DB_4 block
    (0x180200-0x1FFFFF), the shape ``.8hp``/checksum8hp understand."""
    READ_SESSION = _u.READ_SESSION          # 0x4F, as the 6HP28
    SEED_SUB, KEY_SUB = 0x03, 0x04
    READ_ALFID = _u.READ_ALFID              # 0x14
    READ_CHUNK = _u.READ_CHUNK              # 0xF0

    def _unlock_read(self):
        resp = self.client.request(bytes([SID_SECURITY, self.SEED_SUB]))
        if not self.client.is_positive(resp, SID_SECURITY):
            nrc = self.client.nrc_of(resp)
            raise KLineError("read security requestSeed (27 %02X) refused: %s"
                             % (self.SEED_SUB, nrc_name(nrc) if nrc is not None else "no reply"))
        seed = bytes(resp[2:])
        if not seed or not any(seed):
            self._stage("Read security already open (zero seed).")
            return
        key = bytes(_crypto.zf6hp_read_key(seed))
        resp = self.client.request(bytes([SID_SECURITY, self.KEY_SUB]) + key)
        if not self.client.is_positive(resp, SID_SECURITY):
            nrc = self.client.nrc_of(resp)
            raise KLineError("read security sendKey (27 %02X) refused for seed %s / 6HP28 read "
                             "key %s: %s -- this 8HP does not take the 6HP28 read key"
                             % (self.KEY_SUB, seed.hex(), key.hex(),
                                nrc_name(nrc) if nrc is not None else "no reply"))
        self._stage("Read security unlocked (seed %s -> key %s, 6HP28 read key)."
                    % (seed.hex(), key.hex()))

    def read_calibration(self, base=ZF8HP_CAL.base, end=ZF8HP_CAL.base + ZF8HP_CAL.length) -> bytes:
        try:
            self.io.pass_ids()
        except Exception:
            pass
        self._connect()
        self._stage("Read session (10 %02X, the 6HP28 read session; unverified on a real 8HP)..."
                    % self.READ_SESSION)
        self._expect(self.client.request(bytes([SID_SESSION, self.READ_SESSION])),
                     SID_SESSION, "read session 0x%02X" % self.READ_SESSION)
        self._unlock_read()
        total = end - base
        out = bytearray()
        addr = base
        self._stage("Reading 0x%06X-0x%06X (%d KiB) via 0x23 ALFID 0x%02X..."
                    % (base, end - 1, total // 1024, self.READ_ALFID))
        while addr < end:
            if self._is_stop():
                raise KLineError("read aborted (stop requested)")
            n = min(self.READ_CHUNK, end - addr)
            req = bytes([SID_READ_MEM, self.READ_ALFID]) + addr.to_bytes(4, "big") + bytes([n])
            resp = self._read_chunk(req, addr)
            data = bytes(resp[1:])
            if len(data) != n:
                self._stage("  note: @0x%06X asked %d got %d bytes" % (addr, n, len(data)))
                data = data[:n] if len(data) >= n else data + b"\x00" * (n - len(data))
            out += data
            addr += n
            self._percent(int((addr - base) * 100 / total))
        self._stage("Read complete: %d bytes." % len(out))
        return bytes(out)

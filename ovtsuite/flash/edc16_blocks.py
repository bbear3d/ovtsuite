# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The EDC16 flash blocks: one table for the read, the write and the UI.

Each :class:`Block` is one of the flasher's Boot / Program / Calibration / MPC
checkboxes. It says where the block sits in its image, what the read session
uploads for it (``read``, or None when it cannot be read over OBD), and the
write cycles that reprogram it (``writes``: one RequestDownload -> erase ->
TransferData -> transferExit -> checksum each, in the order they are run).

Addresses are the ECU's: the external flash is the 2 MB image from 0x0; the
internal (MPC) flash is addressed from VMA 0x400000 and held as a 0x81000
image whose offset 0 is that VMA. Where the numbers come from (the ODIS
capture, the bench runs) is in ``docs/EDC16_FLASH_BLOCKS.md``.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

#: the external flash image (2 MB from 0x0)
FULL_SIZE = 0x200000
#: the internal flash: VMA base and image size
MPC_VMA = 0x400000
MPC_IMAGE_SIZE = 0x81000

EXTERNAL = "external"
INTERNAL = "internal"


@dataclass(frozen=True)
class WriteSpan:
    """One write cycle: RequestDownload ``size`` bytes at ``addr``, erase
    ``addr..erase_end``, transfer, then checksum ``addr..verify_end`` (both
    inclusive). The data comes from the block's image at ``addr - base``."""
    name: str
    addr: int
    size: int
    erase_end: int
    verify_end: int

    @property
    def end(self) -> int:
        return self.addr + self.size


@dataclass(frozen=True)
class Block:
    key: str                           # "boot" | "program" | "cal" | "mpc"
    label: str
    chip: str                          # EXTERNAL | INTERNAL
    base: int                          # ECU address of the image's offset 0
    region: Tuple[int, int]            # [start, end) the block owns (ECU addresses)
    read: Optional[Tuple[int, int]]    # [start, end) uploaded by a read; None = refused
    writes: Tuple[WriteSpan, ...]
    data: Tuple[int, int]              # [start, end) that must hold data to be writable

    @property
    def readable(self) -> bool:
        return self.read is not None


# Boot/low: refused by the read session at every security level and by both
# read services (0x35 upload, 0x23 readMemoryByAddress). It can be written
# (ODIS block 1), and the 0x31 C5 checksum verifies it.
BOOT = Block(
    "boot", "Boot", EXTERNAL, 0x0, (0x000000, 0x040000), None,
    (WriteSpan("boot", 0x010000, 0x02FF00, 0x03FFFF, 0x03FEFF),),
    (0x010000, 0x03FF00))

# Program: ODIS block 2. The ECU checksums only 0x40000-0x9FFFF; the write
# extent is source-driven up to the descriptor end (edc16_write.program_extent).
# A read takes the whole descriptor, 0xA0000-0xBFFFF included (real data that
# ODIS does not reflash).
PROGRAM_START = 0x040000
PROGRAM_END = 0x0A0000                 # the ODIS / verifiable extent (exclusive)
PROGRAM_GAP_END = 0x0C0000             # 0xA0000-0xBFFFF: data there is not written unless
                                       # data also exists at or above this
PROGRAM_MAX_END = 0x180000             # the program descriptor's end (exclusive)
PROGRAM_SECTOR = 0x040000              # external-flash sector

# Program extent by dataset id (edc16_write.program_extent uses it when the
# container declares no end). Evidence, all in docs/EDC16_FLASH_BLOCKS.md
# ("Program extent by project"): the one EDC16 .sgo in the repo
# (Reference/_incoming/03G906016LQ_0971.sgo, P475AAXE) declares its 0x40000
# section 0x140000 long (end 0x180000); the 295 SGO-unpacked 2 MB images in
# Reference/EDC16-SGO-Unpacked (data can only come from a declared section)
# carry program data in the last 64 KB sector (0x170000-0x17FFFF) for every
# P447, P529 and P475 dataset except the P475 "U8" generation (P475U8A7/D3/D4),
# which, like every P350U8, holds nothing above 0xA0000.
# Keys are the project; a dataset id whose generation (chars 4-5) is in
# PROGRAM_SHORT_GENERATIONS is always the ODIS extent. Projects not listed
# (P422, P213, P216, P450, ...) have no SGO evidence: the scan decides.
PROGRAM_EXTENT_BY_PROJECT = {
    "P350": PROGRAM_END,               # 13 unpacked images, all P350U8*: nothing above 0xA0000
    "P447": PROGRAM_MAX_END,           # 177 unpacked images: last byte 0x17FFFF is data
    "P475": PROGRAM_MAX_END,           # 78 long images; the 14 U8 ones are short, below
    "P529": PROGRAM_MAX_END,           # 12 unpacked images, all with data in 0x170000-0x17FFFF
}
PROGRAM_SHORT_GENERATIONS = ("U8",)    # P350U8*, P475U8*: ODIS extent
PROGRAM = Block(
    "program", "Program", EXTERNAL, 0x0, (PROGRAM_START, PROGRAM_MAX_END),
    (PROGRAM_START, PROGRAM_MAX_END),
    (WriteSpan("program", PROGRAM_START, PROGRAM_END - PROGRAM_START,
               PROGRAM_END - 1, PROGRAM_END - 1),),
    (PROGRAM_START, PROGRAM_END))

# Calibration: ODIS blocks 4 and 5, written in that order (0x1C0000 first).
CAL_A = WriteSpan("cal-A", 0x1C0000, 0x03E000, 0x1FFFFF, 0x1FDFFF)
CAL_B = WriteSpan("cal-B", 0x180000, 0x040000, 0x1BFFFF, 0x1BFFFF)
CAL_START = 0x180000
CAL_SIZE = FULL_SIZE - CAL_START
CAL = Block(
    "cal", "Calibration", EXTERNAL, 0x0, (CAL_START, FULL_SIZE),
    (CAL_START, FULL_SIZE), (CAL_A, CAL_B), (CAL_START, FULL_SIZE))

# MPC (internal flash): ODIS block 3. 0x400000-0x403FFF is refused by the read
# session and never written, so the read starts at 0x404000 and that head is
# left 0xFF. The flash ends at 0x480000; the image's last 4 KiB
# (0x480000-0x480FFF) is padding, not flash, and the ECU refuses it -- so it
# is not asked for either.
MPC_ADDR = 0x404000
MPC_FLASH_END = 0x480000
MPC = Block(
    "mpc", "MPC", INTERNAL, MPC_VMA, (MPC_VMA, MPC_VMA + MPC_IMAGE_SIZE),
    (MPC_ADDR, MPC_FLASH_END),
    (WriteSpan("mpc", MPC_ADDR, 0x07C000, 0x47FFFF, 0x47FFFF),),
    (MPC_ADDR, MPC_ADDR + 0x07C000))

#: every block, in the order a full write runs them (the ODIS order:
#: boot -> program -> MPC -> calibration)
ALL = (BOOT, PROGRAM, MPC, CAL)
BY_KEY = {b.key: b for b in ALL}
#: the UI's checkbox order
UI_ORDER = ("boot", "program", "cal", "mpc")


def get(key) -> Block:
    return BY_KEY[key]


def ordered(keys):
    """``keys`` as Blocks, in the write order (unknown keys are an error)."""
    keys = set(keys)
    unknown = keys - set(BY_KEY)
    if unknown:
        raise KeyError("unknown EDC16 block(s): %s" % ", ".join(sorted(unknown)))
    return [b for b in ALL if b.key in keys]


def _blank(buf, start, end) -> bool:
    """True when [start, end) of ``buf`` is absent or uniformly 0xFF / 0x00."""
    if buf is None or len(buf) < end:
        return True
    from ..edc16.checksum16 import is_blank
    return is_blank(buf[start:end])


def has_data(block: Block, image) -> bool:
    """Whether ``image`` (the block's chip image) holds data for ``block``."""
    a, b = block.data
    return not _blank(image, a - block.base, b - block.base)


#: the smallest internal-flash image taken as whole: tools save 0x80000,
#: 0x80100 / 0x80200 (a shadow row behind it) or the 0x81000 the app works with
MPC_IMAGE_MIN = 0x80000


def pad_mpc(mpc_image):
    """``mpc_image`` as the 0x81000 working image: a 0x80000..0x81000 one is
    cut at the flash's end and padded with 0xFF (the 4 KiB after the flash
    is blank in every image); a longer one is cut; anything shorter, or None,
    comes back unchanged (the caller refuses it)."""
    if mpc_image is None or len(mpc_image) < MPC_IMAGE_MIN:
        return mpc_image
    img = bytes(mpc_image[:MPC_IMAGE_SIZE])
    if len(img) == MPC_IMAGE_SIZE:
        return img
    # a shadow row after 0x80000 is not flash: blank it, then pad
    return img[:MPC_IMAGE_MIN] + b"\xFF" * (MPC_IMAGE_SIZE - MPC_IMAGE_MIN)


def available(image, mpc_image=None) -> dict:
    """{key: bool}: which blocks the source images hold data for (so can be
    written). ``image`` is the 2 MB external flash, ``mpc_image`` the 0x81000
    internal flash."""
    mpc_image = pad_mpc(mpc_image)
    return {b.key: has_data(b, mpc_image if b.chip == INTERNAL else image)
            for b in ALL}


# The cal-only write in Edc16Flash numbers the calibration blocks as the ECU's
# descriptor does: 7 = 0x1C0000, 6 = 0x180000.
CAL_BLOCK_NUMBERS = {7: CAL_A, 6: CAL_B}

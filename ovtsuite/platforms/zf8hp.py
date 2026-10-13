# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r"""ZF 8HP (first generation, VAG "8HPXY"/AL551, 0BK/0B6 gearboxes; TCU box
codes 8K0/8K1/8R0927158x) -- the TCU image format, read from the images
themselves.

STATUS (2026-10-10): format, container (``.8hp``), checksums, identification
and a first definition table are in. An 8HP .frf / 2 MB image / calibration
extract opens with its parameters: every record's kind, dimensions and
element widths are read from the calibration's own self-describing records
(:mod:`.records8hp`); names come from :mod:`.kb8hp` when the image's pointer
table matches a shipped table (50 layouts: every Gen1-family build in the
19-file OEM corpus and the 342-image SGO corpus -- AL551, AL450, ALX51),
otherwise the curves/maps are listed unnamed.
The UDS flasher core (:mod:`ovtsuite.flash.zf8hp_uds`) and the bench
simulator profile "8hp" exist but the car-side choreography is unverified --
see that module. Full account: project doc ``zf8hp_format_and_frf.md``.

THE CONTROLLER: ZF's own TCU ("ITCU2_3_X_2048", per the image's ZFSWINFO
string), a Renesas SuperH SH-2A (big-endian) with 2 MB of flash. Established
from the code itself -- the SH `rts` (0x000B), `sts.l pr,@-r15` (0x4F22) and
`lds.l @r15+,pr` (0x4F26) halfwords dominate the program block while no
PowerPC or TriCore signature appears -- and from the big-endian IEEE floats in
the calibration. Not a Bosch unit like the 6HP's MPC5xx.

THE FLASH MAP (2 MB, every address below read from the blocks' own
descriptors, none guessed):

    0x000000-0x006000  bootloader (never in a field FRF)
    0x006000-0x008000  DB_2  "01DC" boot/loader data        (data 0x1E00 of 0x2000)
    0x008000-0x020000  bootloader, continued (never in a FRF)
    0x020000-0x040000  DB_3  "01DC" flash loader             (data 0x1FE00 of 0x20000)
    0x040000-0x180000  DB_1  application program             (data 0x13FE00 of 0x140000)
    0x180000-0x180200  DB_4 programming-status area (bootloader-written)
    0x180200-0x200000  DB_4  calibration                     (data 0x7FE00)

Each logical block is 0x200 bytes shorter than its erase region: the
difference is a programming-status/validity area the bootloader writes after
a successful download (declared in the block's own header, see
:class:`BlockHeader`), which a flash-delivery file therefore never carries.

THE BLOCK DESCRIPTOR: every block starts with the same ZF header (DB_1/2/3/4
of every file, and the Gen2 4 MB images in Reference/ZF8HP/A2L use it too):

    +0x00  87 65 43 kk       magic; kk = block kind (0xC1 program, 0x30 loader,
                             0x83 calibration)
    +0x0C  u32 BE            absolute address of the block's END MARKER
                             (``12 34 56 78``, the last one in the block)
    +0x10  u32 BE            absolute address of the TYPTEILENR identity record
                             (this block's own at base+0x54, or -- for DB_3 --
                             DB_2's: the two are one logical component)
    +0x1A  "TYPTEILENR" + 8-char component id (e.g. b0ec4444, b1dc1323)
    +0x38  u32 BE            checksum range start (absolute, inclusive)
    +0x3C  u32 BE            checksum range end   (absolute, inclusive)
    +0x44  u32 BE            stored checksum = zlib CRC-32 of that range
    +0x54  u32 BE            programming-status area 1 (addr), +0x58 its size
    +0x5C  u32 BE            programming-status area 2 (addr), +0x60 its size

The block's absolute BASE is ``(+0x0C) - offset_of_last_end_marker_in_block``:
a structural derivation, cross-checked by the checksum range (which must fall
inside the block) and the CRC itself matching. With it, all 76 blocks of the
19-file corpus (18 encrypted + 1 plaintext development build) place at exactly
the four bases above and every stored CRC-32 reproduces.

THE CHECKSUM: plain CRC-32 (IEEE, reflected, init/xorout 0xFFFFFFFF --
``zlib.crc32``) over the header-declared range. The calibration's range starts
at 0x190000, NOT at the block's data start 0x180200: the pointer table and
block header below 0x190000 are outside it. Correcting a tune = recomputing
one 32-bit word per touched block (:mod:`ovtsuite.zf8hp.checksum8hp`).

THE PARAMETER POINTER TABLE: the calibration block's data proper begins with a
table of big-endian 32-bit absolute addresses (from base+0x80 up to a
terminating word outside the block), one per calibration parameter, in the
A2L's own characteristic order. Verified on the Gen2 image in
Reference/ZF8HP/A2L: 14,098 of its 14,288 distinct table entries ARE
CHARACTERISTIC addresses of the matching A2L, in file order. The Gen1 table
has ~9,800 entries. See :func:`pointer_table`.
"""
import re
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

BLOCK_MAGIC = b"\x87\x65\x43"
END_MARKER = b"\x12\x34\x56\x78"
IDENT_TAG = b"TYPTEILENR"
_IDENT_OFF = 0x1A

#: Block kind byte (header +0x03) -> logical block name.
KINDS = {0xC1: "DB_1", 0x30: "DB_2/DB_3", 0x83: "DB_4"}

#: The attested bases, used only as a cross-check / for naming (the base is
#: always derived from the block's own header first).
BASES = {"DB_2": 0x6000, "DB_3": 0x20000, "DB_1": 0x40000, "DB_4": 0x180200}
FLASH_SIZE = 0x200000
CAL_BASE = 0x180200
CAL_LEN = 0x7FE00

#: ZF 8HP Gen2 (ALX52 / "AL552" family: MLBevo Q7/Q8 4M, A6 C8, A4 B9, A8 D5,
#: Bentayga, Touareg). Same ITCU descriptor and self-describing record format
#: as Gen1, a larger ~3 MB flash with different block bases, calibration based
#: at 0x260200 (Gen1: 0x180200). FRF blocks are method 0xAA (AES-128-CBC +
#: LZSS, ovtsuite.flash.crypto); a Gen2 image also carries a kk=0xA1 coding
#: block (DB_05) that is not a based flash block.
GEN2_BASES = {"DB_2": 0x6200, "DB_3": 0x20200, "DB_1": 0x50200, "DB_4": 0x260200}
GEN2_FLASH_SIZE = 0x300000
GEN2_CAL_BASE = 0x260200
GEN2_CAL_LEN = 0x9FE00
#: the 4 MB ALX52 models (Q7/Q8 4M, A6 C8 4K0, A8 D5 4N0, Bentayga 36A,
#: Touareg 760, Q8 4M8/4ML) share the loader/program bases but put the bigger
#: calibration at 0x2E0200; same record format, so the only new value is the base.
GEN2_CAL_BASE_4MB = 0x2E0200
GEN2_CAL_LEN_4MB = 0x11FE00

#: all recognised calibration bases (newest first) and the block length at each
GEN2_CAL_BASES = (GEN2_CAL_BASE, GEN2_CAL_BASE_4MB)
CAL_BASES = (CAL_BASE, GEN2_CAL_BASE, GEN2_CAL_BASE_4MB)
_CAL_LEN_OF = {CAL_BASE: CAL_LEN, GEN2_CAL_BASE: GEN2_CAL_LEN, GEN2_CAL_BASE_4MB: GEN2_CAL_LEN_4MB}
_ALL_BLOCK_BASES = set(BASES.values()) | set(GEN2_BASES.values()) | {GEN2_CAL_BASE_4MB}


def is_block_base(base):
    """True for any ZF 8HP (Gen1 or Gen2) block base."""
    return base in _ALL_BLOCK_BASES


def generation(data: bytes) -> int:
    """2 for a Gen2 (ALX52) image/calibration, 1 for Gen1, from the base its
    calibration descriptor places itself at; defaults to 1."""
    cal = calibration(data)
    return 2 if cal is not None and cal[0] in GEN2_CAL_BASES else 1

_INFO_RE = re.compile(rb"ZF(SW|AD)INFO\*([^\x00\xff]{4,160})")
_PART_RE = re.compile(rb"J217 ([0-9][A-Z0-9]{2}9271[0-9]{2}[A-Z ]{0,2})")
_ZPM_RE = re.compile(rb"ZPM([A-Z0-9]{9})")
_VER_RE = re.compile(rb"\xa5\x5a\x00\x0c([0-9A-Z]{4})\x00\x00\x5a\xa5")


@dataclass
class BlockHeader:
    kind: int                 # header byte +0x03
    end_marker_addr: int      # +0x0C
    ident_addr: int           # +0x10
    component: str            # the 8 chars after TYPTEILENR
    crc_start: int            # +0x38 (absolute, inclusive)
    crc_end: int              # +0x3C (absolute, inclusive)
    crc_stored: int           # +0x44
    status_areas: List[Tuple[int, int]]   # (addr, size) pairs from +0x54..
    base: int                 # derived: end_marker_addr - offset of the marker
    length: int               # bytes of block data the header was read from

    @property
    def name(self) -> str:
        """Logical block name from the base (the kind byte alone can't tell
        DB_2 from DB_3)."""
        for n, b in BASES.items():
            if b == self.base:
                return n
        return KINDS.get(self.kind, "DB_?")

    @property
    def crc_slot(self) -> int:
        """Absolute address of the stored CRC word."""
        return self.base + 0x44


def parse_block_header(block: bytes) -> Optional[BlockHeader]:
    """Read a block's ZF descriptor; ``None`` when ``block`` doesn't start
    with one (never raises). ``block`` is one logical block's plaintext --
    e.g. an FRF DATABLOCK, or a slice of the full image at a block base."""
    if len(block) < 0x80 or block[:3] != BLOCK_MAGIC:
        return None
    if block[_IDENT_OFF:_IDENT_OFF + len(IDENT_TAG)] != IDENT_TAG:
        return None
    end_ptr, ident_ptr = struct.unpack_from(">II", block, 0x0C)
    mk = block.rfind(END_MARKER)
    if mk < 0 or end_ptr < mk:
        return None
    base = end_ptr - mk
    s, e, _ff, ck = struct.unpack_from(">IIII", block, 0x38)
    if not (base <= s <= e < base + len(block)):
        return None
    areas = []
    for off in (0x54, 0x5C):
        a, n = struct.unpack_from(">II", block, off)
        if 0 < n <= 0x10000 and a != 0xFFFFFFFF:
            areas.append((a, n))
    comp = block[_IDENT_OFF + len(IDENT_TAG):_IDENT_OFF + len(IDENT_TAG) + 8]
    return BlockHeader(kind=block[3], end_marker_addr=end_ptr, ident_addr=ident_ptr,
                       component=comp.decode("latin-1"), crc_start=s, crc_end=e,
                       crc_stored=ck, status_areas=areas, base=base, length=len(block))


def block_base(block: bytes) -> Optional[int]:
    """Absolute flash base of a loose block, from its own header."""
    h = parse_block_header(block)
    return None if h is None else h.base


def find_blocks(data: bytes) -> Dict[int, BlockHeader]:
    """Every ZF block descriptor in ``data``, keyed by its offset in ``data``.

    Handles a full (2 MB) image, a loose block, or a calibration-only extract:
    a header is accepted where its own derived base is consistent with where
    it sits -- i.e. ``data`` is either based at 0 (a full image: header at
    offset == base) or IS the block (header at offset 0, any base)."""
    out: Dict[int, BlockHeader] = {}
    # candidate descriptor offsets: a block magic followed by the identity tag
    cands = []
    at = data.find(BLOCK_MAGIC)
    while at != -1:
        if data[at + _IDENT_OFF:at + _IDENT_OFF + len(IDENT_TAG)] == IDENT_TAG:
            cands.append(at)
        at = data.find(BLOCK_MAGIC, at + 1)
    for at in cands:
        # A full image (based at 0): the descriptor's +0x0C field is the
        # ABSOLUTE address of the block's end marker, so for a block that
        # places itself at `at` the marker sits at file offset end_ptr. Slice
        # exactly there (+4) -- structural, both generations, no length table
        # and no risk of a later block's marker. A loose block / calibration
        # extract is the single descriptor at offset 0, parsed whole.
        if at + 0x10 <= len(data):
            end_ptr = struct.unpack_from(">I", data, at + 0x0C)[0]
            if at < end_ptr <= len(data) - 4:
                h = parse_block_header(data[at:end_ptr + 4])
                if h is not None and h.base == at:
                    out[at] = h
                    continue
        if at == 0:
            h = parse_block_header(data)
            if h is not None:
                out[at] = h
    return out


_BLOCK_LENS = {"DB_2": 0x1E00, "DB_3": 0x1FE00, "DB_1": 0x13FE00, "DB_4": 0x7FE00}


def _block_len_for(name: str) -> int:
    return _BLOCK_LENS.get(name, FLASH_SIZE)


def detect(data: bytes) -> bool:
    """True when ``data`` is a ZF 8HP image (Gen1 or Gen2): a full flash image
    with the calibration block's descriptor at a known calibration base (Gen1
    0x180200, Gen2 0x260200), or a loose block / calibration extract starting
    with a descriptor whose derived base is one of the platform's.
    Format-defined (magic + identity tag + a header that parses and places
    itself), not a size or content heuristic -- so it can never capture an
    EDC15/EDC16/6HP image."""
    if len(data) < 0x80:
        return False
    for cb in CAL_BASES:
        cl = _CAL_LEN_OF[cb]
        if len(data) >= cb + 0x80 and data[cb:cb + 3] == BLOCK_MAGIC:
            h = parse_block_header(data[cb:cb + cl])
            if h is not None and h.base == cb:
                return True
    if data[:3] == BLOCK_MAGIC:
        h = parse_block_header(data)
        return h is not None and is_block_base(h.base)
    return False


def is_full_image(data: bytes) -> bool:
    return len(data) >= CAL_BASE + CAL_LEN and detect(data) and data[:3] != BLOCK_MAGIC


def calibration(data: bytes) -> Optional[Tuple[int, bytes]]:
    """``(base, bytes)`` of the calibration block in a full image or a loose
    calibration block; ``None`` if ``data`` holds neither."""
    for cb in CAL_BASES:
        cl = _CAL_LEN_OF[cb]
        if len(data) >= cb + 0x80 and data[cb:cb + 3] == BLOCK_MAGIC:
            blk = data[cb:cb + cl]
            h = parse_block_header(blk)
            if h is not None and h.base == cb:
                return cb, bytes(blk)
    h = parse_block_header(data)
    if h is not None and h.base in CAL_BASES:
        return h.base, bytes(data[:_CAL_LEN_OF[h.base]])
    return None


def pointer_table(cal: bytes, base: int = CAL_BASE) -> List[int]:
    """The calibration's parameter pointer table: big-endian u32 absolute
    addresses pointing inside the block, in the A2L's characteristic order
    (see module docstring). The table begins right after the block header --
    base+0x80 on Gen1, base+0xB0 on the longer Gen2 header -- so its start is
    found structurally: the first word-aligned offset in 0x80..0x100 where a
    run of >=32 in-range addresses begins (Gen1 lands on 0x80 exactly,
    unchanged). Read until the first word outside the block."""
    lo, hi = base, base + len(cal)
    def _in(off):
        return off + 4 <= len(cal) and lo <= struct.unpack_from(">I", cal, off)[0] < hi
    start = None
    for off in range(0x80, 0x100, 4):
        if all(_in(off + 4 * k) for k in range(32)):
            start = off
            break
    if start is None:
        return []
    out: List[int] = []
    off = start
    while _in(off):
        out.append(struct.unpack_from(">I", cal, off)[0])
        off += 4
    return out


def identify(data: bytes) -> Dict[str, str]:
    """Identity strings the image itself carries (any subset may be absent
    in a loose block): ``software`` (ZFSWINFO/ZFADINFO id, e.g. ZX8L4400),
    ``info`` (the whole ZF info string), ``part`` (VAG box code from the
    calibration's 'J217 8K0927158AD' record), ``dataset`` (ZPM... id),
    ``version`` (the a5 5a .. 5a a5 version record, e.g. 1005), and
    ``components`` (TYPTEILENR ids of the blocks present)."""
    out: Dict[str, str] = {}
    m = _INFO_RE.search(data)
    if m:
        info = m.group(2).decode("latin-1")
        out["info"] = info
        out["software"] = info.split("*")[0]
    m = _PART_RE.search(data)
    if m:
        out["part"] = m.group(1).decode("latin-1").strip()
    m = _ZPM_RE.search(data)
    if m:
        out["dataset"] = "ZPM" + m.group(1).decode("latin-1")
    vers = [v.group(1).decode("latin-1") for v in _VER_RE.finditer(data)]
    if vers:
        out["version"] = vers[-1]
        if len(vers) > 1:
            out["versions"] = ",".join(vers)
    comps = sorted({h.component for h in find_blocks(data).values()})
    if comps:
        out["components"] = ",".join(comps)
    return out


#: ISO 14229 identification DIDs a VAG TCU serves, -> the calibration
#: record that holds the value (the I14229APPL_kid_* records of the A2L).
UDS_IDENT_RECORDS = {
    0xF187: "VAG_SWNr (VW spare part number, the box code)",
    0xF189: "VAG_SWIndex (software version; the a5 5a version record)",
    0xF197: "VAG_SysBez (VW system name)",
    0xF19E: "OdxIdentifier (ASAM/ODX file identifier)",
    0xF1A2: "OdxFileVer (ASAM/ODX file version)",
}


def uds_identification(data: bytes) -> Dict[int, bytes]:
    """The identification a TCU running this image answers ``22 Fxxx`` with,
    read from the calibration's own identity records. Located STRUCTURALLY by
    the parameter pointer table, whose gaps are the record sizes -- the same
    two runs on all eight corpus builds (2010-2013):

      [OdxFileVer 6][OdxIdentifier 26][System 10][VAG_SysBez 13][VAG_SysBez 13]
      [SysBezMutter 5][VAG_SWNr 11][VAG_VWTnr 11]

    The first run is the one whose 26-byte record is ASCII "EV_..." between a
    6-byte and a 10-byte record; the second a 5 / 11 / 11-byte run of
    printable records (SysBezMutter is "J217 " on series builds and a ZF code
    such as "Y4CT " on the Bentley development builds). F189 is the version record after the calibration's end
    marker (:func:`identify`'s ``version``). Values are the stored bytes
    (fixed-length, padding kept). Absent records give absent DIDs. The
    hardware number (F191) and hardware version (F1A3) are not in the flash
    image -- a TCU keeps them outside the downloadable blocks."""
    out: Dict[int, bytes] = {}
    cal = calibration(data)
    if cal is None:
        return out
    base, blob = cal
    ptrs = pointer_table(blob, base)
    if not ptrs:
        return out
    s = sorted(set(ptrs))
    gap = {a: b - a for a, b in zip(s, s[1:])}

    def rec(a):
        o = a - base
        return bytes(blob[o:o + gap[a]])

    if base in GEN2_CAL_BASES:
        # Gen2 (ALX52) keeps the same records but in a different pointer layout:
        # VAG_SWNr is a 12-byte record (11 chars + NUL) not adjacent to the
        # "J217 " designation, and OdxIdentifier's neighbours differ. Match by
        # each record's own format: the box-code (a VAG part number) and the
        # "EV_" OdxIdentifier with the OdxFileVer record right before it.
        _PN = re.compile(rb"^[0-9A-Z]{2,3}9[0-9]{5}[0-9A-Z]{0,2}$")
        for i in range(1, len(s)):
            a = s[i]; g = gap.get(a)
            if g == 26 and gap.get(s[i - 1]) == 6:
                v = rec(a)
                if v.startswith(b"EV_"):
                    out[0xF19E] = v
                    out[0xF1A2] = rec(s[i - 1])
            if g in (11, 12) and 0xF187 not in out and _PN.match(rec(a).rstrip(b"\x00 ")):
                out[0xF187] = rec(a).rstrip(b"\x00")
        ver = identify(data).get("version")
        if ver:
            out[0xF189] = ver.encode("latin-1")
        return out

    for i in range(1, len(s) - 2):
        a = s[i]
        g = gap.get(a)
        if g == 26 and gap.get(s[i - 1]) == 6 and gap.get(s[i + 1]) == 10 and gap.get(s[i + 2]) == 13:
            v = rec(a)
            if v.startswith(b"EV_") and all(32 <= c < 127 or c == 0 for c in v):
                out[0xF19E] = v
                out[0xF1A2] = rec(s[i - 1])
                out[0xF197] = rec(s[i + 2])
        elif (g == 5 and gap.get(s[i + 1]) == 11 and gap.get(s[i + 2]) == 11
              and all(32 <= c < 127 for c in rec(a) + rec(s[i + 1]))):
            # [component designation 5 ("J217 "; ZF codes on dev builds)][VAG_SWNr 11][VAG_VWTnr 11]
            out[0xF187] = rec(s[i + 1])
    ver = identify(data).get("version")
    if ver:
        out[0xF189] = ver.encode("latin-1")
    return out

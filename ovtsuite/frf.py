# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
r'''VAG "FRF" flash-container support -- read-only extraction of the file
format modern ODIS/GFF-era VAG diagnostic tools use to deliver flash
software. Built for the SAME ZF 6HP TCUs OVT-Suite already talks to live
over CAN (:mod:`ovtsuite.flash.zf6hp`) and stores as ``.sgo``
(:mod:`ovtsuite.sgo`) -- this is a THIRD delivery format for the same
calibration data, used by newer UDS-based tooling. No write path: nothing
here builds a new .frf.

ZF 8HP (added 2026-10-09): the same outer layer wraps the first-generation
ZF 8HP TCU's FRFs (box codes 8K0/8K1/8R0927158x, software 0BK910255x), but
their inner blocks are tagged ``ENCRYPT-COMPRESS-METHOD 22`` -- a different
codec from the 6HP's "11"/BCB, solved from a plaintext ("00") development
build in the same corpus and shipped as
:func:`ovtsuite.flash.crypto.zf8hp_frf_decode` (see that module's comment
block for the full account and the OEM-exact encoder). Every 8HP block carries
ZF's own block descriptor (:mod:`ovtsuite.zf8hp`), which states the block's
absolute base; :func:`image` uses it exactly as it uses the 6HP's BCB Type1
``[START_ADDRESS]`` header, so an 8HP FRF assembles into its true 2 MB flash
layout with no inference. The SA2 tape in every 8HP FRF seen (19 files) is
byte-identical to :data:`ovtsuite.flash.crypto.ZF6HP_WRITE_TAPE`.

Two nested layers, both structural facts learned by reading BSD-licensed
bri3d/VW_Flash (https://github.com/bri3d/VW_Flash, BSD 2-Clause -- see that
project's own LICENSE; its ``lib/lzss/`` subdirectory is separately
GPL-licensed and NOT used by, ported into, or needed by anything in this
module) and independently re-verified against 5 real 6HP .frf files on disk
(3 box codes -- 8K0927158A/B/C -- 5 software versions), never just trusted:

1. OUTER (:func:`decrypt`): the whole .frf file is XORed with a public,
   non-secret repeating keystream (``data/frf.key``, 4095 bytes -- the SAME
   bytes VW_Flash ships in its own ``data/frf.key``, used for every VAG FRF
   regardless of ECU, not a per-file or per-ECU secret) via a "recursive
   xor" construction: each output byte depends on a running state seeded
   from the key bytes consumed so far, not a plain repeating-XOR. Decrypting
   yields a plain ZIP file -- verified: starts with the ZIP local-file
   signature ``PK\x03\x04`` for all 5 real 6HP FRFs checked -- containing
   one ODX-FLASH XML document.

2. INNER (:func:`_decode_block`): the ODX XML's ``<FLASHDATA>`` elements
   hold, per logical block (``DB_1``..``DB_4`` for every 6HP file seen), a
   hex-encoded blob tagged with an ``<ENCRYPT-COMPRESS-METHOD>`` code ("11"
   for every real 6HP block with actual data; "00" tags the paired *ERASE*
   entries, which carry no data and are skipped). Despite the method name,
   this is NOT a new cipher: each block's own leading bytes carry the SAME
   "[SOURCE]/[CHECKSUM]/[FORMAT]" plaintext provenance header and ``1A 01``
   BCB marker already known from ``.sgo``'s own crypt=1 sections (see
   :mod:`ovtsuite.sgo` and :func:`ovtsuite.flash.crypto.zf6hp_bcb_decode`'s
   own docstring) -- decoded with that EXACT, already-shipped function, not
   a new algorithm. Verified two independent ways per block, for every
   block in all 5 real 6HP FRFs on hand: (a) decoded length matches the
   block's own declared ``UNCOMPRESSED-SIZE`` exactly, and (b)
   ``zlib.crc32`` of the decoded plaintext matches the ODX's own
   ``<SECURITY-METHOD>CRC32F</SECURITY-METHOD><FW-CHECKSUM>`` value for that
   block, byte for byte -- the file's OWN self-declared checksum, not a
   value this module invents. Both must pass for a block to be accepted;
   either failing leaves it unrecognized (``plaintext=None``) rather than
   guessed, same philosophy as :mod:`ovtsuite.sgo`.

SEED/KEY (:func:`FrfContainer.sa2_tape` / ``sa2_tape_is_known_write_tape``):
every ODX's ``<SECURITY>`` block with ``<SECURITY-METHOD>SA2</SECURITY-METHOD>``
carries a 12-byte ``<FW-SIGNATURE>`` "tape". For every 6HP FRF checked (5
files, 3 box codes, 5 software versions) this tape is byte-for-byte
IDENTICAL to :data:`ovtsuite.flash.crypto.ZF6HP_WRITE_TAPE` -- the SAME SA2
write-security tape already solved and verified (18/18 real seed/key pairs)
for the older K-line/CAN-TP2.0 6HP family (see :mod:`ovtsuite.flash.zf6hp`
and project memory ``zf6hp-flash-transport``). So these newer UDS-based 6HP
TCUs use the exact same write security-access algorithm already shipped --
:func:`sa2_execute`/:func:`zf6hp_write_key` need no changes; this module
just confirms the match per file rather than assuming it, and flags a
container plainly if a future file's tape ever differs.

ADDRESSING: each ``DATABLOCK``'s ``<SOURCE-START-ADDRESS>`` is a small
logical block index (1-4 for every file seen -- it is the UDS block
identifier the download is addressed by), NOT a byte offset into a flat
image. The blocks' true absolute bases come from the blocks themselves:
the 6HP's BCB Type1 ``[START_ADDRESS]`` header, or the 8HP's ZF block
descriptor (:mod:`ovtsuite.zf8hp`). :func:`image` assembles the flat flash
image from those anchors (see its comment block); a block with neither
anchor is placed below the lowest anchored one and reported as
``inferred`` rather than silently trusted.
'''
import io
import os
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import xml.etree.ElementTree as ET

from .platforms import zf6hp as _zf6hp
from .platforms import zf8hp as _zf8hp
from .flash import crypto as _crypto

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_FRF_KEY_PATH = os.path.join(_DATA_DIR, "frf.key")

_ZIP_SIG = b"PK\x03\x04"


def _frf_key() -> bytes:
    with open(_FRF_KEY_PATH, "rb") as fh:
        return fh.read()


def decrypt(data: bytes, key: Optional[bytes] = None) -> bytes:
    """Undo the outer whole-file FRF cipher (see module docstring point 1).
    A "recursive xor": ported statement-for-statement from bri3d/VW_Flash's
    BSD-licensed ``frf/decryptfrf.py`` (``decrypt_data``), not re-derived --
    the algorithm's construction (running ``first_seed``/``second_seed``
    state mixed with each key byte) isn't something a differently-shaped
    reimplementation would happen to land on by accident, so this is
    reused code, credited, not an independent rediscovery."""
    if key is None:
        key = _frf_key()
    out = bytearray(len(data))
    key_index = 0
    first_seed = 0
    second_seed = 1
    klen = len(key)
    for i, data_byte in enumerate(data):
        key_byte = key[key_index]
        first_seed = ((first_seed + key_byte) * 3) & 0xFF
        out[i] = data_byte ^ (first_seed ^ 0xFF ^ second_seed ^ key_byte)
        second_seed = ((second_seed + 1) * first_seed) & 0xFF
        key_index += 1
        if key_index >= klen:
            key_index = 0
    return bytes(out)


def detect(data: bytes, key: Optional[bytes] = None) -> bool:
    """True when ``data`` decrypts (see :func:`decrypt`) to something
    starting with the ZIP local-file signature. Cheap: the cipher is a
    strictly sequential stream, so only the first 4 bytes need decrypting
    to check this -- verified equal to decrypting the whole file first."""
    if len(data) < 4:
        return False
    return decrypt(data[:4], key) == _ZIP_SIG


def unwrap(data: bytes, key: Optional[bytes] = None):
    """Decrypt and unzip a .frf file. Returns ``(odx_bytes, inner_name)``
    for the single ODX-FLASH document every real 6HP FRF checked contains.
    Raises ``ValueError`` if decryption doesn't yield a valid ZIP, or the
    ZIP doesn't contain exactly one file."""
    decrypted = decrypt(data, key)
    if decrypted[:4] != _ZIP_SIG:
        raise ValueError("frf.unwrap: decrypted data is not a ZIP file "
                          "(bad frf.key, or not a .frf file)")
    with zipfile.ZipFile(io.BytesIO(decrypted)) as zf:
        names = zf.namelist()
        if len(names) != 1:
            raise ValueError(
                "frf.unwrap: expected exactly one file inside the FRF's "
                "ZIP, found %d: %r" % (len(names), names))
        return zf.read(names[0]), names[0]


@dataclass
class FrfBlock:
    id: str                    # ODX FLASHDATA element ID
    short_name: str            # e.g. "DB_1"
    source_index: int          # DATABLOCK's SOURCE-START-ADDRESS -- a
                                # logical block index, NOT a byte address
                                # (see module docstring's ADDRESSING note)
    declared_len: int          # DATABLOCK SEGMENT's UNCOMPRESSED-SIZE
    method: str                # raw ENCRYPT-COMPRESS-METHOD code, e.g. "11"
    start_address: Optional[int] = None   # BCB Type1 [START_ADDRESS]: the
                                          # block's true absolute flash base
    end_address: Optional[int] = None     # BCB Type1 [END_ADDRESS] (inclusive)
    scheme: Optional[str] = None
    plaintext: Optional[bytes] = None
    crc_expected: Optional[int] = None
    crc_actual: Optional[int] = None

    @property
    def crc_ok(self) -> bool:
        return (self.crc_expected is not None
                and self.crc_actual == self.crc_expected)

    @property
    def looks_like_zf6hp_calibration(self) -> bool:
        return self.plaintext is not None and _zf6hp.detect(self.plaintext)

    @property
    def zf8hp_header(self):
        """The ZF 8HP block descriptor at the start of this block's
        plaintext (:func:`ovtsuite.zf8hp.parse_block_header`), or None."""
        if self.plaintext is None:
            return None
        return _zf8hp.parse_block_header(self.plaintext)

    @property
    def looks_like_zf8hp(self) -> bool:
        """True for any block of a ZF 8HP image (it carries ZF's descriptor
        and places itself at one of the platform's block bases)."""
        h = self.zf8hp_header
        return h is not None and _zf8hp.is_block_base(h.base)

    @property
    def looks_like_zf8hp_calibration(self) -> bool:
        h = self.zf8hp_header
        return h is not None and h.base in _zf8hp.CAL_BASES

    @property
    def is_calibration(self) -> bool:
        """The block a cal-only tune edits, on whichever platform."""
        return self.looks_like_zf6hp_calibration or self.looks_like_zf8hp_calibration


@dataclass
class FrfContainer:
    name: str
    ecu_id: str                                    # ODX FLASH element ID
    ident_values: Dict[str, List[str]]              # EXPECTED-IDENT short-name -> values
    blocks: List[FrfBlock] = field(default_factory=list)
    sa2_tape: Optional[bytes] = None

    @property
    def sa2_tape_is_known_write_tape(self) -> Optional[bool]:
        """None if this container has no SA2 tape at all; otherwise whether
        it's byte-identical to the already-solved
        :data:`ovtsuite.flash.crypto.ZF6HP_WRITE_TAPE` (see module
        docstring's SEED/KEY section)."""
        if self.sa2_tape is None:
            return None
        return self.sa2_tape == _crypto.ZF6HP_WRITE_TAPE

    @property
    def unrecognized(self) -> List[FrfBlock]:
        return [b for b in self.blocks if b.scheme is None]

    @property
    def platform(self) -> Optional[str]:
        """"zf6hp" / "zf8hp" from the decoded blocks' own structure, None when
        no block identifies a platform."""
        if any(b.looks_like_zf8hp for b in self.blocks):
            return "zf8hp"
        if any(b.looks_like_zf6hp_calibration for b in self.blocks):
            return "zf6hp"
        return None

    @property
    def calibration_blocks(self) -> List[FrfBlock]:
        return [b for b in self.blocks if b.is_calibration]

    @property
    def spare_part_number(self) -> Optional[str]:
        for key in ("EI_VWSparePartNumbe",):
            vals = self.ident_values.get(key)
            if vals:
                return vals[0].strip()
        return None


def _parse_bcb_header(prefix: bytes) -> Dict[str, str]:
    """Key/value pairs from a Bosch "BCB Type1" ASCII header -- the text that
    precedes the ``1A01`` compressed stream in each FLASHDATA block. Lines look
    like ``[KEY]`` on their own, the value on the next line, e.g.
    ``[START_ADDRESS]\n20080``. Returns ``{}`` for anything that isn't such a
    header (never raises)."""
    out: Dict[str, str] = {}
    try:
        lines = prefix.decode("latin-1").split("\n")
    except Exception:
        return out
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if len(ln) >= 2 and ln.startswith("[") and ln.endswith("]"):
            out[ln[1:-1]] = lines[i + 1].strip() if i + 1 < len(lines) else ""
            i += 2
        else:
            i += 1
    return out


def _verified(decoded, declared_len: int, crc_expected: Optional[int], scheme: str):
    """Accept a candidate plaintext only on declared length AND (when the ODX
    carries one) its own CRC32F; otherwise (None, None)."""
    if decoded is None or len(decoded) != declared_len:
        return None, None
    if crc_expected is not None:
        if (zlib.crc32(decoded) & 0xFFFFFFFF) != crc_expected:
            return None, None
    return bytes(decoded), scheme


def _decode_block(raw: bytes, declared_len: int, crc_expected: Optional[int],
                  method: str = ""):
    """(plaintext, scheme) for one FLASHDATA block's decrypted hex payload,
    or (None, None) if it doesn't verify -- see module docstring point 2.
    Never accepted on length alone; the CRC32F cross-check (when the ODX
    carries one, which every block seen does) must also match.

    Dispatch is on the ODX's own ENCRYPT-COMPRESS-METHOD code, each code's
    codec confirmed on real files: "00" plaintext (an 8HP development
    build), "22" the 8HP XOR+LZSS codec, "11" the 6HP BCB stream. A code
    this module does not know is still TRIED against the BCB marker (the
    6HP's "11" blocks were first recognized that way, by their plaintext
    header, before the code's meaning was known) and otherwise left
    unrecognized rather than guessed."""
    if method == "00":
        return _verified(raw, declared_len, crc_expected, "plain")
    if method == "22":
        try:
            decoded = _crypto.zf8hp_frf_decode(raw, declared_len)
        except ValueError:
            decoded = None
        return _verified(decoded, declared_len, crc_expected, "lz22")
    if method == "AA":
        # ZF 8HP Gen2 (ALX52): AES-128-CBC then 10-bit-distance LZSS
        try:
            decoded = _crypto.zf8hp_gen2_frf_decode(raw, declared_len)
        except (ValueError, RuntimeError):
            decoded = None
        return _verified(decoded, declared_len, crc_expected, "aes_lz")
    idx = raw.find(_crypto.ZF6HP_BCB_MARKER)
    if idx == -1:
        return None, None
    try:
        decoded = _crypto.zf6hp_bcb_decode(raw[idx:])
    except ValueError:
        return None, None
    return _verified(decoded, declared_len, crc_expected, "bcb")


def parse_odx(odx_bytes: bytes, name: str = "") -> FrfContainer:
    """Structural parse of one ODX-FLASH document (already unwrapped from
    its .frf/.zip -- see :func:`unwrap`), decoding every FLASHDATA block it
    can (self-verified, see :func:`_decode_block`)."""
    root = ET.fromstring(odx_bytes)
    flash_el = root.find("./FLASH")
    ecu_id = flash_el.get("ID") if flash_el is not None else ""

    ident_values: Dict[str, List[str]] = {}
    for ei in root.findall(
            "./FLASH/ECU-MEMS/ECU-MEM/MEM/SESSIONS/SESSION/EXPECTED-IDENTS/EXPECTED-IDENT"):
        sn = ei.findtext("./SHORT-NAME") or ""
        vals = [v.text for v in ei.findall("./IDENT-VALUES/IDENT-VALUE") if v.text]
        if sn:
            ident_values[sn] = vals

    # CRC32F cross-checks, keyed by the DATA block's short name (e.g. "DB_4").
    crc_by_db: Dict[str, int] = {}
    sa2_tape: Optional[bytes] = None
    for sec in root.findall(".//SECURITY"):
        method = sec.findtext("./SECURITY-METHOD")
        # Gen1 8HP/6HP tag the per-block zlib CRC "CRC32F"; Gen2 8HP (ALX52)
        # tags the same zlib.crc32(plaintext) value "CRC32" -- accept both.
        if method in ("CRC32F", "CRC32"):
            vf = sec.findtext("./VALIDITY-FOR")
            chk = sec.findtext("./FW-CHECKSUM")
            if vf and chk:
                crc_by_db[vf] = int(chk, 16)
        elif method == "SA2":
            sig = sec.findtext("./FW-SIGNATURE")
            if sig:
                sa2_tape = bytes.fromhex(sig)

    # DATABLOCK -> (source_index, declared_len) per FLASHDATA-REF, skipping
    # ERASE-typed blocks (no data, matched separately by the "ERASE" ID
    # suffix already present in the ODX's own naming).
    db_by_flashdata_ref: Dict[str, tuple] = {}
    for db in root.findall("./FLASH/ECU-MEMS/ECU-MEM/MEM/DATABLOCKS/DATABLOCK"):
        if db.get("TYPE") == "ERASE":
            continue
        ref_el = db.find("./FLASHDATA-REF")
        seg = db.find("./SEGMENTS/SEGMENT")
        if ref_el is None or seg is None:
            continue
        ref = ref_el.get("ID-REF")
        src = seg.findtext("./SOURCE-START-ADDRESS")
        ulen = seg.findtext("./UNCOMPRESSED-SIZE")
        short_name = db.findtext("./SHORT-NAME") or ""
        db_by_flashdata_ref[ref] = (short_name, int(src, 16) if src else 0,
                                     int(ulen) if ulen else 0)

    blocks: List[FrfBlock] = []
    for fd in root.findall("./FLASH/ECU-MEMS/ECU-MEM/MEM/FLASHDATAS/FLASHDATA"):
        fid = fd.get("ID")
        meta = db_by_flashdata_ref.get(fid)
        if meta is None:
            continue                                 # an ERASE-only entry
        short_name, source_index, declared_len = meta
        method = fd.findtext("./ENCRYPT-COMPRESS-METHOD") or ""
        content = fd.findtext("./DATA") or ""
        block = FrfBlock(id=fid, short_name=short_name, source_index=source_index,
                          declared_len=declared_len, method=method)
        if len(content) > 2:
            raw = bytes.fromhex(content)
            _mi = raw.find(_crypto.ZF6HP_BCB_MARKER)
            if _mi > 0:
                _hdr = _parse_bcb_header(raw[:_mi])
                for _k, _attr in (("START_ADDRESS", "start_address"),
                                  ("END_ADDRESS", "end_address")):
                    if _hdr.get(_k):
                        try:
                            setattr(block, _attr, int(_hdr[_k], 16))
                        except ValueError:
                            pass
            crc_expected = crc_by_db.get(short_name)
            plaintext, scheme = _decode_block(raw, declared_len, crc_expected, method)
            block.plaintext = plaintext
            block.scheme = scheme
            block.crc_expected = crc_expected
            if plaintext is not None:
                block.crc_actual = zlib.crc32(plaintext) & 0xFFFFFFFF
                # ZF 8HP: the block's own descriptor states its absolute
                # base (end-marker pointer minus the marker's offset) -- the
                # 8HP equivalent of the 6HP's BCB Type1 [START_ADDRESS].
                if block.start_address is None:
                    _h8 = _zf8hp.parse_block_header(plaintext)
                    if _h8 is not None:
                        block.start_address = _h8.base
                        block.end_address = _h8.base + len(plaintext) - 1
        blocks.append(block)

    return FrfContainer(name=name, ecu_id=ecu_id, ident_values=ident_values,
                         blocks=blocks, sa2_tape=sa2_tape)


# ---------------------------------------------------------------------------
# Full-image assembly (true flash layout)
# ---------------------------------------------------------------------------
# A .frf carries the flashable blocks (program + calibration) but NOT the
# bootloader -- field update packages never reflash it. Each block that has a
# descriptor states its own absolute flash address through that descriptor's
# +0x08 pointer (the matching end-descriptor's absolute location), so the true
# 2 MB layout is read from the data, not guessed: e.g. for 8K0927158x the
# descriptors place DB_2 @0x40000, DB_1 @0x80000 and DB_4 @0x180000, and the
# DB_4 directory's own absolute addresses (0x180907..) land inside the DB_4
# region of the assembled image -- an independent cross-check. Every block also
# carries a Bosch "BCB Type1" ASCII header with a [START_ADDRESS]/[END_ADDRESS]
# pair; that header is the primary, structural anchor -- it agrees with the
# binary descriptors where both exist, and resolves DB_3 (which has no binary
# descriptor) to its true base 0x20080. Only a block with neither a header
# address nor a descriptor is inferred (placed below the lowest anchored block)
# and reported in ``inferred``. The region below the
# lowest block is the bootloader the FRF does not carry, reported in ``absent``
# and left as 0xFF -- a real OBD read (PCMFlash etc.) would have it.
_IMG_BANK = 0x20000                     # 128 KiB: the smallest flash bank here
_DESC_TAG = re.compile(rb"DA[CR][0-9A-Z]{5}|COBC[0-9A-Z]{4}")


@dataclass
class FrfImage:
    data: bytes                         # the assembled flat image
    size: int
    placements: Dict[str, int]          # block short-name -> absolute offset
    inferred: List[str]                 # blocks placed without a descriptor
    absent: List[tuple]                 # (start, end) ranges the FRF omits (bootloader)


def _block_base(block: "FrfBlock") -> Optional[int]:
    """A block's absolute flash base from its own descriptor pair, or None.

    The first descriptor's +0x08 field points at the matching end descriptor's
    ABSOLUTE address; subtracting the end descriptor's offset within the block
    gives the block's base. Returns None when the block carries no descriptor
    pair (e.g. DB_3) or the pointer is implausible."""
    d = block.plaintext
    if not d:
        return None
    tags = [m.start() for m in _DESC_TAG.finditer(d)]
    if len(tags) < 2:
        return None
    first, last = tags[0], tags[-1]
    if first + 12 > len(d):
        return None
    end_ptr = struct.unpack_from(">I", d, first + 8)[0]
    base = end_ptr - last
    if base < 0 or base >= 0x4000000 or base % 0x1000 != 0:
        return None
    return base


def image(container: FrfContainer) -> FrfImage:
    """Assemble the FRF's blocks into the full flat flash image at their true
    addresses. Raises ValueError if no block is descriptor-anchored."""
    blocks = [b for b in container.blocks if b.plaintext is not None]
    bases: Dict[str, int] = {}
    # Primary anchor: the BCB Type1 [START_ADDRESS] header every block carries
    # -- a structural, format-defined field. Where a binary descriptor also
    # exists it agrees (an independent cross-check); the header is authoritative.
    for b in blocks:
        if b.start_address is not None:
            bases[b.short_name] = b.start_address
    # Fall back to the binary descriptor pair for any block without a header.
    for b in blocks:
        if b.short_name not in bases:
            bb = _block_base(b)
            if bb is not None:
                bases[b.short_name] = bb
    if not bases:
        raise ValueError("no descriptor- or header-anchored block to position the image")
    inferred: List[str] = []
    unplaced: List[str] = []
    # Any block with neither a header address nor a descriptor: stack it into
    # the banks just below the lowest anchored base (deterministic, by index).
    for b in sorted((b for b in blocks if b.short_name not in bases),
                    key=lambda b: b.source_index):
        banks = max(1, (len(b.plaintext) + _IMG_BANK - 1) // _IMG_BANK)
        base = min(bases.values()) - banks * _IMG_BANK
        if base < 0:
            # No descriptor and no room below the anchored blocks: a block with
            # no fixed flash address (e.g. the ZF 8HP Gen2 kk=0xA1 coding/variant
            # block). Guessing an address would be worse than leaving it out, so
            # record it unplaced rather than inventing one or failing the image.
            unplaced.append(b.short_name)
            continue
        bases[b.short_name] = base
        inferred.append(b.short_name)
    placed = [b for b in blocks if b.short_name in bases]
    size = max(bases[b.short_name] + len(b.plaintext) for b in placed)
    size = (size + _IMG_BANK - 1) // _IMG_BANK * _IMG_BANK
    img = bytearray(b"\xFF" * size)
    for b in placed:
        off = bases[b.short_name]
        img[off:off + len(b.plaintext)] = b.plaintext
    # Regions the FRF does not carry: the area below the lowest block and any
    # gap between blocks of at least one 4 KB page (the 6HP's 128-byte
    # sector-alignment pads between blocks are not "absent" content; the 8HP's
    # bootloader is split around its DB_2 loader-data block, 0-0x6000 and
    # 0x8000-0x20000, and both halves are).
    spans = sorted((bases[b.short_name], bases[b.short_name] + len(b.plaintext))
                   for b in placed)
    absent: List[tuple] = []
    cursor = 0
    for s, e in spans:
        if s - cursor >= 0x1000:
            absent.append((cursor, s))
        cursor = max(cursor, e)
    return FrfImage(bytes(img), size, dict(bases), inferred + unplaced, absent)


def parse(data: bytes, name: str = "", key: Optional[bytes] = None) -> FrfContainer:
    """Unwrap and parse a whole .frf file (see :func:`unwrap`/:func:`parse_odx`)."""
    odx_bytes, inner_name = unwrap(data, key)
    return parse_odx(odx_bytes, name=name or inner_name)

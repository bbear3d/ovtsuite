# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VDO Mk4 cluster EEPROM: the format-defined layout anchors.

Base-app knowledge (no definitions): the two physical layouts, the checksum
the cluster enforces, the immobilizer-ID field, the patch-module header and the
ROM version encoding. Enough to say "this 2 KB file is a VWK503-layout VDO
cluster EEPROM", and for the flasher to refuse a cross-layout write, with the
VDO module absent. Naming the exact ROM family and building parameters is the
VDO module's job (:mod:`ovtsuite.vdo.identify`, :mod:`ovtsuite.vdo.defs`).
"""

import re
from dataclasses import dataclass
from typing import Optional

EEPROM_SIZE = 2048

#: The physical layouts, keyed by layout name: where the format-defined
#: anchors live. ``checksum`` = (checksum byte, [(region, length), ...]).
LAYOUTS = {
    "VWK501": {"checksum": (0x14E, ((0x14F, 8), (0x220, 0x20))),
               "immo_id": 0xA2, "patch": 0x4F4},
    "VWK503": {"checksum": (0x190, ((0x191, 8), (0x258, 0x20))),
               "immo_id": 0xE0, "patch": 0x4F6},
}

#: The immobilizer ID's own format: two letters, Z, digit, Z, 0, letter,
#: seven digits -- e.g. VWZ7Z0B6127535 (same pattern kw1281test keys its
#: EEPROM-format detection on).
IMMO_ID_RE = re.compile(rb"[A-Z]{2}Z\dZ0[A-Z]\d{7}")
#: Characters a virgin / blanked immobilizer ID is filled with.
_BLANK_IMMO = (b"X" * 14, b"\xFF" * 14, b"\x00" * 14)

#: Vehicle-family nibble -> chassis (KWHack Reference/Distance_Impulse_Number.md;
#: confirmed on 20 samples by KWHack, Skoda from the same document). These are
#: the families the VWK501/503 (Mk4-layout) clusters are configured for:
#: Golf/Jetta/Bora (the VW "A4" platform, 0x0), Passat B5 (0x1 / 0xA) and
#: Skoda. "A4" here is the VW Mk4 platform, NOT the Audi A4 (B5-platform Audi,
#: which carries C5-family cluster hardware: see :data:`C5_IMMO_ADDR` and
#: :func:`is_c5_cluster`), so nothing in this table is an Audi chassis. The
#: "B5" key is what the definitions' ``options_by_chassis`` is keyed on.
CHASSIS = {0x0: "Golf/Jetta (A4 platform)", 0x1: "B5", 0xA: "B5",
           0x5: "Skoda", 0xB: "Skoda"}

#: Audi C5-family cluster (4B0920/4Z7920/8D0920/8Z0920): an older, structurally
#: different EEPROM this base app does not have a full layout for (no checksum
#: formula, no patch header). Identified instead the same way the module-plan
#: Reference/XDFs/Audi920.xdf anchors it and the way Tester.cs's own redundant
#: SKC check does: three identical 14-byte copies of the immobilizer-ID
#: pattern, verified against a real cluster's dump (owner's own bench unit,
#: 2026-11-15) -- at 0x726, 0x734, 0x742, not the Mk4 layouts' single field.
C5_IMMO_ADDR = 0x726
C5_IMMO_STRIDE = 14
C5_IMMO_COPIES = 3


def checksum_ok(data: bytes, layout: str) -> bool:
    addr, regions = LAYOUTS[layout]["checksum"]
    s = data[addr] + sum(sum(data[a:a + n]) for a, n in regions)
    return (s & 0xFF) == 0xFF


def immo_id_ok(data: bytes, layout: str) -> bool:
    a = LAYOUTS[layout]["immo_id"]
    first = bytes(data[a:a + 14])
    return bool(IMMO_ID_RE.fullmatch(first)) or first in _BLANK_IMMO


def is_c5_cluster(data: bytes) -> bool:
    """True when ``data`` carries a C5-family cluster's triple-redundant
    immobilizer-ID pattern (0x726/0x734/0x742): all three copies equal AND a
    well-formed immobilizer ID. Unlike the Mk4 layouts' single field, the
    blanked-pattern sentinels are NOT accepted here -- with no real blank C5
    sample to confirm one, that would make an all-0xFF/0x00 blob of the right
    size (no cluster EEPROM at all) match every time (three equal copies of
    anything blank), exactly what :func:`detect` promises never to do."""
    if len(data) != EEPROM_SIZE:
        return False
    copies = [bytes(data[C5_IMMO_ADDR + i * C5_IMMO_STRIDE:
                         C5_IMMO_ADDR + i * C5_IMMO_STRIDE + 14])
              for i in range(C5_IMMO_COPIES)]
    if len(set(copies)) != 1:
        return False
    return bool(IMMO_ID_RE.fullmatch(copies[0]))


def layout_of(data: bytes) -> Optional[str]:
    """The physical layout, or None when ``data`` is not a cluster EEPROM.

    A Mk4 layout needs BOTH of its anchors (checksum valid, immobilizer-ID
    field well-formed). When an edit has broken the checksum, the
    immobilizer field alone decides -- but only if exactly one layout has
    it, so a file that matches neither cleanly is never claimed. A C5-family
    cluster (:func:`is_c5_cluster`) has no Mk4 anchors at all and is
    reported as ``"C5"`` -- not a key in :data:`LAYOUTS`, since this base
    app has no checksum formula or patch-header address for it."""
    if len(data) != EEPROM_SIZE:
        return None
    full = [lay for lay in LAYOUTS
            if checksum_ok(data, lay) and immo_id_ok(data, lay)]
    if len(full) == 1:
        return full[0]
    if full:
        return None
    immo = [lay for lay in LAYOUTS if immo_id_ok(data, lay)
            and IMMO_ID_RE.fullmatch(bytes(data[LAYOUTS[lay]["immo_id"]:
                                                LAYOUTS[lay]["immo_id"] + 14]))]
    if len(immo) == 1:
        return immo[0]
    if is_c5_cluster(data):
        return "C5"
    return None


# ---------------------------------------------------------------- ROM versions
def rom_bytes(version: str) -> Optional[bytes]:
    """'VWK501MH 01.10' -> b'VWK501MH' + bytes([0x10, 0x01]) (the 10 bytes the
    cluster's ReadSoftwareVersion returns: 8 ASCII, then minor, then major --
    the sticker prints them major.minor)."""
    m = re.fullmatch(r"(.{8}) ([0-9A-Fa-f]{2})\.([0-9A-Fa-f]{2})", version or "")
    if not m:
        return None
    return m.group(1).encode("latin-1") + bytes([int(m.group(3), 16),
                                                 int(m.group(2), 16)])


def version_string(raw: bytes) -> str:
    """The inverse of :func:`rom_bytes`, for a 10-byte version block."""
    raw = bytes(raw)
    if len(raw) != 10:
        return " ".join("$%02X" % b for b in raw)
    return "%s %02X.%02X" % (raw[:8].decode("latin-1"), raw[9], raw[8])


def header_rom_match(h4: int, h5: int, version: str) -> bool:
    """Does patch header (H4, H5) target ROM ``version``? PatchModule.md."""
    rb = rom_bytes(version)
    if rb is None or h4 != rb[8]:
        return False
    if not h5 & 0x80:
        return h5 == rb[9]
    return h5 == ((sum(rb[:8]) + rb[9]) & 0xFF) | 0x80


@dataclass
class PatchHeader:
    address: int
    h: bytes                  # H0..H5
    code_len: int
    data_len: int
    valid: bool               # H0 verifies and the sizes obey the rules
    computed_h0: int

    @property
    def body_end(self) -> int:
        return self.address + 6 + self.code_len + self.data_len


def patch_checksum(block: bytes) -> int:
    """H0 for a patch block (header + code + data): the byte sum of H1 (low 7
    bits), H2..H5 and the body; complemented when it would equal H1's low 7
    bits (so an empty/erased header can never verify). PatchModule.md;
    verified on every sample and every published needle-sweep patch."""
    h1 = block[1] & 0x7F
    n = (block[2] | (block[3] << 8)) + h1
    s = (h1 + sum(block[2:6]) + sum(block[6:6 + n])) & 0xFF
    return (~s) & 0xFF if s == h1 else s


def patch_header(data: bytes, address: Optional[int],
                 end: int = 0x7E0) -> Optional[PatchHeader]:
    """The patch-module header at ``address``, or None when there is none
    (erased: H0 == H1, the module's own "no patch" test, or sizes that cannot
    fit). ``valid`` means the cluster would load it."""
    if address is None or address + 6 > len(data):
        return None
    h = bytes(data[address:address + 6])
    if h[0] == h[1]:
        return None
    data_len = h[1] & 0x7F
    code_len = h[2] | (h[3] << 8)
    if address + 6 + code_len + data_len > end:
        return None
    total = code_len + data_len
    rules = (total > 0 and total % 2 == 0 and data_len > 3
             and data_len % 4 == 2)
    computed = patch_checksum(bytes(data[address:end]))
    return PatchHeader(address=address, h=h, code_len=code_len,
                       data_len=data_len, valid=rules and computed == h[0],
                       computed_h0=computed)


def detect(data) -> bool:
    """True when ``data`` is a VDO Mk4 cluster EEPROM (exactly 2 KB, with one
    layout's structural anchors present). Never claims anything else: every
    other platform's image is orders of magnitude larger."""
    if data is None or len(data) != EEPROM_SIZE:
        return False
    return layout_of(bytes(data)) is not None

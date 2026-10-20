# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP TCU images: format-defined identification (base-app knowledge).

The ZF 5HP19 (VW "01V") and 5HP24/24A (VW "01L") Bosch transmission ECUs are
512 KiB 68k images laid out as two self-identifying blocks, each headed by the
same ``BT010405`` marker the firmware uses: a CODE block at 0x8000
(``BT010405<sw>-CODE-``) and a DATA/calibration block at 0x60000
(``BT010405<sw>...``). The boot sector below 0x8000 is not carried by the
``.sgo`` delivery files and is left blank in the flat image. The gearbox code
in the ident block near 0x70384 (``AG5 01V`` / ``AG5 01L``) names the family.

This is a structural test -- two fixed block headers -- not a size or content
guess, and it never overlaps the other platforms' descriptors (verified on the
5HP19 and RS6 5HP24 reference images). Container decode (the GEHEIM BCB Type1
``.sgo``), the checksum and the definitions live in the ZF 5HP module and in
``ovtsuite.sgo``; only recognition is here. See project doc
``zf5hp19_feasibility.md``.
"""

import re
from typing import Dict

_CODE_HDR = 0x8000       # code block header
_DATA_HDR = 0x60000      # data/calibration block header
_IDENT = 0x70360         # ident block (part number + gearbox + engine);
                         # start early -- the block shifts a little between
                         # BT format versions (PN at 0x7038E on BT010405,
                         # 0x70378 on BT010504)
_MARK = b"BT"            # ZF Bosch block-header prefix; the 6 chars after
                         # are a BT format version (010405, 010504, ...),
                         # so it is NOT pinned -- see _CODE_TAG below
_CODE_TAG = b"-CODE-"    # structural tag at the code-block header +0x0E

_PN = re.compile(rb"[0-9A-Z]{2}0927156[0-9A-Z]{0,3}")
_GEARBOX = re.compile(rb"AG[45] 01[VL]")

#: VW gearbox code -> ZF family name.
_FAMILY = {b"01V": "5HP19", b"01L": "5HP24"}


def _header_at(data: bytes, off: int) -> bool:
    return len(data) >= off + 0x14 and data[off:off + 2] == _MARK


def detect(data: bytes) -> bool:
    # Two ``BT``-prefixed block headers (code at 0x8000, data at 0x60000), with
    # the code block carrying the fixed ``-CODE-`` tag at +0x0E. The 6 version
    # chars after ``BT`` vary by image (BT010405, BT010504, ...), so detection
    # keys on the structural tag, not the version -- verified across 5HP19
    # software levels E46722/E35722 (BT010405) and G35208 (BT010504).
    return (_header_at(data, _CODE_HDR)
            and data[_CODE_HDR + 0x0E:_CODE_HDR + 0x14] == _CODE_TAG
            and _header_at(data, _DATA_HDR))


def swap16(data: bytes) -> bytes:
    """Exchange the two bytes of every 16-bit word (even-length images)."""
    b = bytearray(len(data))
    b[0::2] = data[1::2]
    b[1::2] = data[0::2]
    return bytes(b)


def byte_swapped(data: bytes) -> bool:
    """True when a 5HP image was read with the two bytes of every 16-bit word
    exchanged, as 16-bit-wide bench/BDM readers save it. Structural, not a
    guess: the image is byte-swapped exactly when it is not a native 5HP image
    but becomes one once swapped (the ``BT010405`` block headers decide)."""
    data = bytes(data)
    if len(data) % 2 or detect(data):
        return False
    return detect(swap16(data))


def describe(data: bytes) -> Dict[str, str]:
    """Family / part number / gearbox facts for the identity line."""
    out: Dict[str, str] = {}
    seg = data[_IDENT:_IDENT + 0x60]
    gb = _GEARBOX.search(seg)
    if gb:
        out["family"] = _FAMILY.get(gb.group(0)[-3:], "5HP")
        out["variant"] = gb.group(0).decode("latin-1")
    pn = _PN.search(seg)
    if pn:
        out["part"] = pn.group(0).decode("latin-1").strip()
    return out

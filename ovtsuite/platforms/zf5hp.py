# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP TCU images: format-defined identification (base-app knowledge).

The ZF 5HP19 (VW "01V") and 5HP24/24A (VW "01L") Bosch transmission ECUs are
68k images laid out as two self-identifying ``BT``-headed blocks, in two size classes:

* **512 KiB** (AM29F400-class): a CODE block at 0x8000 (``BT010405<sw>-CODE-``,
  version chars vary -- BT010405, BT010504) and a DATA/calibration block at
  0x60000; ident near 0x70380.
* **256 KiB** (AM29F200-class, older units -- e.g. A6 C5 2.5 TDI): a CODE block
  at 0x8000 (``BT010204<sw>``, NO ``-CODE-`` tag) and a DATA block at 0x36C00;
  ident near 0x3BF80. Both headers carry the same ``BT<ver><sw>``.

The boot sector below 0x8000 is not carried by the ``.sgo`` delivery files and
is left blank in the flat image. The gearbox code in the ident block
(``AG5 01V`` / ``AG5 01L``) names the family.

This is a structural test -- two block headers -- not a size or content guess,
and it never overlaps the other platforms' descriptors (verified on 512K
E46722/E35722/G35208 and 256K C22935 images). Container decode (the GEHEIM BCB
Type1 ``.sgo``), the checksum and the definitions live in the ZF 5HP module and
in ``ovtsuite.sgo``; only recognition is here. The 256 KiB checksum layout is
not worked out yet, so checksum declines for that size. See project docs
``zf5hp19_feasibility.md`` and ``zf5hp_corpus_additions_2026_10_20.md``.
"""

import re
from typing import Dict

_MARK = b"BT"            # ZF Bosch block-header prefix; the 6 chars after
                         # are a BT format version (010405, 010504, 010204 ...)
_CODE_TAG = b"-CODE-"    # structural tag at the code-block header +0x0E (512K)

#: Per-size layout profiles: ``(code_hdr, data_hdr, has_-CODE-_tag)``.
#: 512 KiB (AM29F400-class): BT010405/010504, a CODE block at 0x8000 with the
#: ``-CODE-`` tag and a DATA block at 0x60000, ident near 0x70380.
#: 256 KiB (AM29F200-class, older 2.5 TDI etc.): BT010204, a CODE block at
#: 0x8000 with NO ``-CODE-`` tag and a DATA block at 0x36C00, ident near
#: 0x3BF80. Both block headers carry the same ``BT<ver><sw>`` (12 chars).
_CODE_HDR = 0x8000       # the code block header always sits here
_SIZES = (0x80000, 0x40000)   # 512 KiB (AM29F400) and 256 KiB (AM29F200) classes


def _data_hdr(data: bytes) -> int:
    """Offset of the DATA/calibration block header, found DYNAMICALLY: a second
    copy of the code block's ``BT<ver><sw>`` header (14 bytes) later in the
    image. Fixed at 0x60000 on 512 KiB images, but at a VARYING offset on
    256 KiB ones (0x34A00 / 0x35A00 / 0x36C00 seen), so it must be located, not
    assumed. -1 when there is no second header."""
    if len(data) < _CODE_HDR + 0x0E or data[_CODE_HDR:_CODE_HDR + 2] != _MARK:
        return -1
    sig = data[_CODE_HDR:_CODE_HDR + 0x0E]      # "BT" + version(6) + software(6)
    return data.find(sig, _CODE_HDR + 0x1000)

# VAG auto-trans TCU part number: 3 prefix chars + "927156" + up to 3
# suffix letters. The 3rd prefix char is NOT always 0 (3B0/8E0/4B0 end in
# 0, but 4Z7 does not), so match three prefix chars, not two-plus-zero.
_PN = re.compile(rb"[0-9A-Z]{3}927156[0-9A-Z]{0,3}")
_GEARBOX = re.compile(rb"AG[45] 01[VL]")

#: VW gearbox code -> ZF family name.
_FAMILY = {b"01V": "5HP19", b"01L": "5HP24"}


def detect(data: bytes) -> bool:
    """A ZF 5HP flash, by its two matching ``BT`` block headers: a CODE block at
    0x8000 and a DATA block carrying the same ``BT<ver><sw>`` later in the image
    (the 512 KiB layout also has a ``-CODE-`` tag; the 256 KiB one does not).
    Two identical ``BT``+version+software headers is a signature no other
    platform produces. Verified on 512 KiB (E46722/E35722/G35208 etc.) and
    256 KiB (C22935 and the varying-offset A6/A4 units)."""
    if len(data) not in _SIZES:
        return False
    return _data_hdr(data) >= 0


def cal_range(data: bytes):
    """``(lo, hi)`` of the calibration/data region (where the self-describing
    tables live): from the data block header to the image tail. On 512 KiB the
    checksum tail is left off (hi = 0x78000)."""
    dat = _data_hdr(data)
    if dat < 0:
        return (0x60000, 0x78000)
    return (dat, 0x78000 if len(data) == 0x80000 else len(data))


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
    """Family / part number / gearbox facts for the identity line. The ident
    block sits at a size-dependent offset (0x70380 on 512K, 0x3BF80 on 256K),
    so find the ``AG5 01x`` gearbox code anywhere and read the part number from
    just before it."""
    out: Dict[str, str] = {}
    gb = _GEARBOX.search(data)
    if gb:
        out["family"] = _FAMILY.get(gb.group(0)[-3:], "5HP")
        out["variant"] = gb.group(0).decode("latin-1")
        pn = _PN.search(data[max(0, gb.start() - 48):gb.start()])
        if pn:
            out["part"] = pn.group(0).decode("latin-1").strip()
    return out

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Aisin TCU images: format-defined identification (base-app knowledge).

Two families, each recognised by the checksum descriptor the firmware itself
reads -- the same structure the flasher and the Aisin module's checksum use:

* **TR-60SN / VW 09D** (1 MB flash): a 16-byte descriptor at 0x203A0,
  ``[count=1][start=0x2000][end=0xDFFFF][checksum]`` (big-endian). A full
  image is based at 0; the write payload (``[0x2000, 0xDFFFF]``, what the
  flasher transfers) is based at 0x2000, so the same descriptor sits at
  0x1E3A0. See project doc ``aisin_09d_container_and_flash.md``.
* **TR-80SD / VW 0C8, 09G** (AL991/AQ250/AQ450): every flash block ends in a
  16-byte trailer ``[count=1][start][end][value]`` whose ``end`` is the byte
  before the trailer. A flat image places block 0 at 0x040000 (trailer at
  0x13FFF0) and block 1 at 0x005000 (trailer at 0x03FFF0). See project doc
  ``trx80_aisin_8speed_frf_map.md``.
"""

import re
import struct
from typing import Dict, Optional

#: 09D checksum descriptor: offset in a full (0-based) image, and the region
#: it must declare.
D09_DESC = 0x203A0
D09_START = 0x2000
D09_END = 0xDFFFF

#: TR-80 block trailers in a flat image: (trailer offset, start, end).
TR80_TRAILERS = ((0x13FFF0, 0x040000, 0x13FFEF), (0x03FFF0, 0x005000, 0x03FFEF))

_PN_09D = re.compile(rb"09D927750[A-Z ]{0,2}")
_PN_TR80 = re.compile(rb"(0C892775[05]|09G927158)[A-Z ]{0,2}")


def _desc(data: bytes, off: int):
    if off < 0 or len(data) < off + 16:
        return None
    return struct.unpack_from(">IIII", data, off)


def base_09d(data: bytes) -> Optional[int]:
    """The 09D image's base address (0 for a full image, 0x2000 for the write
    payload), or None when ``data`` is not a 09D image."""
    for base in (0, D09_START):
        d = _desc(data, D09_DESC - base)
        if d is None:
            continue
        count, start, end, _ck = d
        if (count, start, end) == (1, D09_START, D09_END) \
                and len(data) >= end + 1 - base:
            return base
    return None


def is_tr80(data: bytes) -> bool:
    for off, start, end in TR80_TRAILERS:
        d = _desc(data, off)
        if d is None or d[:3] != (1, start, end) or end + 1 != off:
            return False
    return True


def detect(data: bytes) -> bool:
    return base_09d(data) is not None or is_tr80(data)


def describe(data: bytes) -> Dict[str, str]:
    """Family / part number / variant facts for the identity line."""
    out: Dict[str, str] = {}
    if base_09d(data) is not None:
        out["family"] = "TR-60SN (09D)"
        m = _PN_09D.search(data)
        if m:
            out["part"] = m.group(0).decode("latin-1").strip()
        if base_09d(data) == D09_START:
            out["variant"] = "write payload (0x2000-0xDFFFF)"
    elif is_tr80(data):
        out["family"] = "TR-80SD (0C8/09G)"
        m = _PN_TR80.search(data)
        if m:
            out["part"] = m.group(0).decode("latin-1").strip()
    return out

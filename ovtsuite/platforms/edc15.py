# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC15 images: format-defined identification (base-app knowledge).

Three generations are recognised, each by the signature its calibration
datasets start with:

* EDC15VM+ / EDC15P (512 KB, C167): ``F9 67 FF FF FF FF FF FF "V4.1"`` on a
  4 KB boundary.
* MSA15 / EDC15V (256 KB, C167): ``55 55 00 00 56 56 33 33 2E 2E 3x 3x`` --
  "UV3.x" with every byte written twice ("V3.1", "V3.0" on the oldest
  028906021C* software) -- at the start of a 32 KB block.
* The first EDC15P software (512 KB, 1998-99: 038906019AM, 045906019Q):
  ``55 55 00 00 50 50 32 32 2E 2E 3x 3x`` -- "UP2.x", doubled the same way --
  at the start of a 32 KB block. Its tables are the EDC15P ones.
"""

import re

#: The V4.1 dataset signature every EDC15VM+/P calibration dataset starts with.
DATASET_SIG = b"\xF9\x67" + b"\xFF" * 6 + b"V4.1"

#: The first ten bytes of an MSA15/EDC15V dataset; the version digit follows,
#: doubled ("11" = V3.1, "00" = V3.0).
DATASET_SIG_V3 = b"\x55\x55\x00\x00\x56\x56\x33\x33\x2E\x2E"
V3_SIZE = 0x40000          # the MSA15/EDC15V flash (two 29F200-class halves)
V3_BLOCK = 0x8000          # one dataset

#: The first ten bytes of a dataset of the first EDC15P software; the version
#: digit follows, doubled ("00" = P2.0). Sets are 32 KB, as on MSA15.
DATASET_SIG_P2 = b"\x55\x55\x00\x00\x50\x50\x32\x32\x2E\x2E"
P2_SIZE = 0x80000


def v3_version(data: bytes, off: int) -> str:
    """"V3.1" / "V3.0" when an MSA15/EDC15V dataset starts at ``off``, else ""."""
    n = len(DATASET_SIG_V3)
    if data[off:off + n] != DATASET_SIG_V3 or off + n + 2 > len(data):
        return ""
    a, b = data[off + n], data[off + n + 1]
    if a != b or not 0x30 <= a <= 0x39:
        return ""
    return "V3.%c" % a


def p2_version(data: bytes, off: int) -> str:
    """"P2.0" when a dataset of the first EDC15P software starts at ``off``."""
    n = len(DATASET_SIG_P2)
    if data[off:off + n] != DATASET_SIG_P2 or off + n + 2 > len(data):
        return ""
    a, b = data[off + n], data[off + n + 1]
    if a != b or not 0x30 <= a <= 0x39:
        return ""
    return "P2.%c" % a


def datasets_p2(data: bytes):
    """Offsets of every dataset of the first EDC15P software (32 KB blocks of
    the 512 KB flash: 0x00000, 0x08000 and 0x78000 on 038906019AM, only
    0x78000 on 045906019Q)."""
    if len(data) != P2_SIZE:
        return []
    return [off for off in range(0, len(data), V3_BLOCK) if p2_version(data, off)]


def datasets_v3(data: bytes):
    """Offsets of every MSA15/EDC15V dataset (on 32 KB boundaries)."""
    if len(data) != V3_SIZE:
        return []
    return [off for off in range(0, len(data), V3_BLOCK) if v3_version(data, off)]


def detect(data: bytes) -> bool:
    """True when ``data`` is an EDC15 image: a V4.1 dataset signature on a 4 KB
    boundary in a plausible C167 flash size, or an MSA15/EDC15V 256 KB image
    with a V3 dataset on a 32 KB boundary, or a 512 KB image of the first
    EDC15P software with a P2 one."""
    if datasets_v3(data) or datasets_p2(data):
        return True
    if len(data) < 0x40000 or len(data) > 0x100000:
        return False
    for off in range(0, len(data) - len(DATASET_SIG), 0x1000):
        if data[off:off + len(DATASET_SIG)] == DATASET_SIG:
            return True
    return False


def is_v3(data: bytes) -> bool:
    """An MSA15/EDC15V image (no V4.1 dataset)."""
    return bool(datasets_v3(data))


def datasets(data: bytes):
    """Offsets of every dataset signature: V4.1 ones on 4 KB boundaries, or
    the MSA15/EDC15V ones, or the first EDC15P software's P2 ones."""
    v3 = datasets_v3(data)
    if v3:
        return v3
    v41 = [off for off in range(0, len(data) - len(DATASET_SIG), 0x1000)
           if data[off:off + len(DATASET_SIG)] == DATASET_SIG]
    return v41 or datasets_p2(data)


# MSA15 / EDC15V idents ("038906018S  1,9l R4 EDC  SG  1621"): they read only
# one way round, so they tell a byte-swapped file from a straight one
_IDENT_V3 = re.compile(rb"\d,\dl R\d EDC|\d\.\dl R\d EDC")


def swap16(data: bytes) -> bytes:
    """``data`` with the two bytes of every 16-bit word exchanged."""
    out = bytearray(len(data))
    out[0::2], out[1::2] = data[1::2], data[0::2]
    return bytes(out)


def _headers(data: bytes, bases) -> int:
    """Little-endian ``[RAM pointer][count]`` table headers in the sets."""
    n = 0
    for b in bases:
        for a in range(b, b + V3_BLOCK - 4, 2):
            p = data[a] | data[a + 1] << 8
            c = data[a + 2] | data[a + 3] << 8
            if 0xC000 <= p <= 0xFDFF and 2 <= c <= 32:
                n += 1
    return n


def byte_swapped(data: bytes) -> bool:
    """True when an MSA15 / EDC15V image was saved with the two bytes of every
    16-bit word exchanged, as some readers write it. The doubled-byte dataset
    signature reads the same either way round, so the ident decides (and,
    in an image with none, the table headers' RAM pointers)."""
    data = bytes(data)
    bases = datasets_v3(data)
    if not bases:
        return False
    sw = swap16(data)
    a = sum(1 for b in bases if _IDENT_V3.search(data, b, b + V3_BLOCK))
    s = sum(1 for b in bases if _IDENT_V3.search(sw, b, b + V3_BLOCK))
    if a or s:
        return s > a
    return _headers(sw, bases) > 2 * _headers(data, bases)

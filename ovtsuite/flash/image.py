# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Normalize a flashable EDC16 image and build a session from a read.

The K-line flash reaches only the top 512 KB calibration window (0x180000-
0x1FFFFF). Per the user's spec, the flash tool accepts a 512 KB, 1 MB or 2 MB
file as long as *the last 512 KB is a valid EDC16 calibration binary* (carries
the 0xCAFECADE-anchored checksum layout). That last 512 KB is placed at
0x180000 over a 0xFF pad to make the full 2 MB image the writer needs; a 2 MB
input is kept whole (its lower 1.5 MB may hold a real boot-mode code read).
"""
from ..modules import registry as _registry
from ..platforms import edc16 as _edc16

CAL_START = 0x180000
CAL_SIZE = 0x080000
FULL_SIZE = 0x200000


class ImageError(Exception):
    pass


def normalize(data: bytes):
    """Return a full 2 MB EDC16 image built from ``data`` (512 KB / 1 MB / 2 MB).

    Raises :class:`ImageError` if the size is unsupported or the last 512 KB
    isn't a recognizable EDC16 calibration binary.
    """
    n = len(data)
    if n == FULL_SIZE:
        candidate = bytearray(data)
    elif n in (CAL_SIZE, 0x100000):
        cal = data[-CAL_SIZE:]                       # last 512 KB
        candidate = bytearray(b"\xFF" * CAL_START) + bytearray(cal)
        candidate += b"\xFF" * (FULL_SIZE - len(candidate))
    else:
        raise ImageError(
            "Expected a 512 KB, 1 MB or 2 MB file; got %d bytes (0x%X). The "
            "flash writes the top 512 KB calibration region." % (n, n))

    # Structural test in the base app (the calibration marker in the window
    # the flash writes); with the EDC16 module installed, its checksum layout
    # must be readable too.
    cs = _registry.checksum("edc16")
    if (_edc16.find_cal_marker(bytes(candidate), CAL_START, FULL_SIZE) < 0
            or (cs is not None and not cs.is_supported(bytes(candidate)))):
        raise ImageError(
            "The last 512 KB isn't a recognizable EDC16 calibration image (no "
            "0xCAFECADE-anchored checksum layout). If this is an EDC15 file it "
            "can't be written to an EDC16.")
    return bytes(candidate)


def checksum_summary(image: bytes) -> str:
    """One-line stored-checksum status for the flash dialog."""
    cs = _registry.checksum("edc16")
    if cs is None:
        return "checksum: not checked (%s)" % _registry.missing_text(
            "edc16", "checksum correction").rstrip(".")
    try:
        rep = cs.verify(image)
    except Exception:
        return "checksum: not checked (unrecognized layout)"
    return "checksum: valid" if rep.ok else "checksum: MISMATCH (auto-corrected on write)"

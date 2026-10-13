# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ``.8hp`` native container's metadata block.

A ``.8hp`` is the common OVT block-directory container (:mod:`ovtsuite.container`,
magic ``OVT8HP``) holding a ZF 8HP (Gen1) image as ``BLOCK_EXTERNAL`` -- either
the full 2 MB flash (the shape a FRF assembles to, or an OBD read with the
bootloader) or a 512 KB calibration extract -- plus the change log and this
``BLOCK_META`` JSON. The meta carries what the image itself declares
(:func:`ovtsuite.zf8hp.identify`), the block map that was present when the file
was written, and where the image came from, so a reader can tell a FRF-derived
image (bootloader absent, 0xFF) from a real TCU read without re-deriving it.
"""
import datetime

from .. import branding as _branding
from .zf8hp import find_blocks as _find_blocks, identify as _identify, FLASH_SIZE, CAL_BASE


def describe_image(data: bytes) -> dict:
    """The platform facts of ``data`` the container records."""
    data = bytes(data)
    m = {"platform": "zf8hp", "image_size": len(data)}
    m.update(_identify(data))
    blocks = {}
    for off, h in sorted(_find_blocks(data).items()):
        blocks[h.name] = {"base": h.base, "length": h.length, "component": h.component,
                          "crc_range": [h.crc_start, h.crc_end]}
    m["blocks"] = blocks
    if len(data) >= FLASH_SIZE:
        m["shape"] = "full"
        m["bootloader_present"] = data[:0x6000] != b"\xff" * 0x6000
    elif "DB_4" in blocks:
        m["shape"] = "calibration"
        m["flash_base"] = CAL_BASE
    else:
        m["shape"] = "block"
    return m


def fill_meta(c, session) -> None:
    """Refresh ``c``'s meta from ``session`` before a save."""
    m = c.meta()
    m.update(describe_image(bytes(session.binfile.data)))
    frf = getattr(session, "_frf_container", None)
    if frf is not None and getattr(frf, "name", None):
        m.setdefault("source_frf", frf.name)
    m.setdefault("source", _branding.APP_NAME)
    m.setdefault("saved_first", datetime.datetime.now().isoformat(timespec="seconds"))
    c.set_meta(m)

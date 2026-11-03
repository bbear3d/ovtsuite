# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC16 images: format-defined identification (base-app knowledge).

An EDC16 image carries its calibration-block descriptor, ``CAFEAFFE``, in
every external-flash image and in the decoded MPC internal flash. That is the
positive test: EDC16 used to be the fallback for anything nothing else
claimed, which forced unrelated files through the EDC16 matcher and failed
the load. Now a binary is EDC16 only when it says so.
"""

#: The EDC16 calibration-block descriptor marker.
DESCRIPTOR = b"\xCA\xFE\xAF\xFE"


def detect(data: bytes) -> bool:
    return data.find(DESCRIPTOR) != -1


#: The calibration-area marker, 0xCAFECADE stored little-endian. Every VW
#: EDC16U1/U31/U34 calibration starts with it (0x18003D on a 2 MB image).
CAL_MARKER = b"\xDE\xCA\xFE\xCA"


#: The calibration window of a 2 MB EDC16 image (what the K-line flash
#: reaches), and the whole flash.
CAL_START = 0x180000
CAL_SIZE = 0x080000
FULL_SIZE = 0x200000


def is_cal_only(data: bytes) -> bool:
    """A 512 KB calibration-only EDC16 file: exactly the 512 KB window, with
    the calibration descriptor and marker in it. (A 1 MB file is more likely
    an EDC16U1's whole flash, and is never taken for one.)"""
    return (len(data) == CAL_SIZE and detect(data)
            and find_cal_marker(data) >= 0)


def pad_cal_only(data: bytes) -> bytes:
    """The full 2 MB image of a calibration-only file: the 512 KB at
    ``CAL_START`` over ``0xFF`` (the same image the flasher writes,
    :func:`ovtsuite.flash.image.normalize`)."""
    return b"\xFF" * CAL_START + bytes(data)


def find_cal_marker(data: bytes, start: int = 0, end: int = None) -> int:
    """Offset of the calibration marker within ``[start, end)``, or -1."""
    return data.find(CAL_MARKER, start, len(data) if end is None else end)


#: The EDC16U31/U34 internal (MPC5xx) flash: 512 KiB of PowerPC code, a vector
#: table at 0, and the CAFEAFFE descriptor at 0x403C -- as a plain file it is
#: 0x81000 bytes (4 KiB of 0xFF after the flash), some tools save 0x80100.
MPC_MIN, MPC_MAX = 0x80000, 0x81000
MPC_DESCRIPTOR_AT = 0x403C
MPC_IDENT_AT = 0x4010


def is_mpc(data: bytes) -> bool:
    """An EDC16 internal-flash (MPC) image, by structure: the right size, a
    PowerPC exception table at 0 (``b``/``bl`` words every 8 bytes, a zero word
    between them), and the descriptor at 0x403C. Told from an external flash
    by the vector table, which no calibration image has at its start."""
    if not MPC_MIN <= len(data) <= MPC_MAX:
        return False
    if data[MPC_DESCRIPTOR_AT:MPC_DESCRIPTOR_AT + 8] != b"\xFA\xDE\xCA\xFE" + DESCRIPTOR:
        return False
    for i in range(0, 0x40, 8):
        w = int.from_bytes(data[i:i + 4], "big")
        if w >> 26 != 18 or data[i + 4:i + 8] != b"\0\0\0\0":
            return False
    return True


def mpc_ident(data: bytes) -> dict:
    """``{"hw": "1037391503", "software": "P475AAXE"}`` from the ident string
    at 0x4010 (empty when it is not there)."""
    raw = bytes(data[MPC_IDENT_AT:MPC_IDENT_AT + 18])
    if len(raw) == 18 and raw[:10].isdigit() and raw[10:].isalnum():
        return {"hw": raw[:10].decode(), "software": raw[10:].decode()}
    return {}

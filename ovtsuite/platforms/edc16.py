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
    if data[:0x4000] == b"\xFF" * 0x4000:
        # an .sgo's internal flash and an OBD read both start at 0x404000:
        # the vector area below is left erased, and the descriptor decides
        return mpc_ident(data) != {}
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


#: The forms an MPC image is saved in, as ``(key, size or None, label)``: the
#: plain image padded to 0x81000 (4 KiB of 0xFF after the flash), the same cut
#: to 0x80100 (some tools) or to the bare 512 KiB flash, and the compressed
#: ``.MPC`` (XOR-0xD9 over a zlib stream plus a 4-byte trailer, as the BDM
#: tools of the Dimsport family write it; see :mod:`ovtsuite.mpc`).
MPC_FORMATS = (
    ("plain81000", 0x81000, "Plain image, 0x81000 bytes (.mpc / .bin)"),
    ("plain80100", 0x80100, "Plain image, 0x80100 bytes (.mpc / .bin)"),
    ("plain80000", 0x80000, "Plain image, 0x80000 bytes (.mpc / .bin)"),
    ("encoded", None, "Compressed .MPC (XOR / zlib)"),
)


def mpc_format_of(raw: bytes) -> dict:
    """How the MPC file ``raw`` is stored: ``{"format": key, "trailer": hex}``
    (the trailer only for the compressed form); {} when it is no MPC."""
    from .. import mpc as _mpc
    if _mpc.is_encoded(raw):
        try:
            plain, trailer = _mpc.decode(raw)
        except Exception:
            return {}
        if is_mpc(plain):
            return {"format": "encoded", "trailer": trailer.hex()}
        return {}
    if is_mpc(raw):
        key = {n: k for k, n, _l in MPC_FORMATS if n}.get(len(raw))
        return {"format": key} if key else {}
    return {}


def decode_mpc(raw: bytes):
    """``(plain image, format)`` of an MPC file in either form (``format`` as
    :func:`mpc_format_of`), or None when ``raw`` is not one."""
    from .. import mpc as _mpc
    fmt = mpc_format_of(raw)
    if not fmt:
        return None
    if fmt["format"] == "encoded":
        return _mpc.decode(raw)[0], fmt
    return bytes(raw), fmt


def mpc_as(plain: bytes, fmt: str, trailer: str = "") -> bytes:
    """The MPC image ``plain`` written in format ``fmt`` (a key of
    :data:`MPC_FORMATS`). A plain size cuts or pads (0xFF) the image to that
    size; the compressed form wraps the 0x81000 image, with ``trailer`` (hex,
    from :func:`mpc_format_of`) when the file came compressed."""
    from .. import mpc as _mpc
    img = bytes(plain)
    sizes = {k: n for k, n, _l in MPC_FORMATS if n}
    n = sizes.get(fmt, 0x81000)
    if len(img) < n:
        img += b"\xFF" * (n - len(img))
    img = img[:n]
    if fmt == "encoded":
        return _mpc.encode(img, bytes.fromhex(trailer or ""))
    return img

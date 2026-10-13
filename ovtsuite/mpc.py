# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Decode / encode the "compressed" .MPC internal-flash images produced by
BDM reads of EDC16 (MPC5xx) ECUs -- e.g. the Dimsport KESS/K-TAG family and
DimExp's own chunk 1 inside a .dim container (see :mod:`ovtsuite.dim`).

FORMAT (derived from a write/readback known-plaintext pair -- see
``docs/CONTAINERS.md``)::

    file = XOR(0xD9, zlib_stream) || trailer[4]

* The whole file is XOR'd byte-wise with the constant 0xD9. De-XOR'd, the
  file starts with ``78 9C`` -- a standard zlib header (CM=8/deflate,
  CINFO=7/32K window, default compression level).
* The payload is therefore a plain RFC1950 zlib stream (RFC1951 deflate
  inside, with the trailing Adler-32 of the decompressed data). No
  proprietary compression, no proprietary cipher.
* 4 bytes follow the end of the zlib stream. Their meaning is not
  identified; they are not needed to decode, and are carried through
  unchanged by :func:`decode`/:func:`encode` so a re-encoded file is
  bit-for-bit interchangeable with the tool that produced the original.

Decoded, the EDC16U31 internal-flash image is ``0x81000`` bytes: a 512 KiB
MPC5xx flash image (PowerPC, vector table at 0) followed by 4 KiB of
``0xFF``. That is the same size ``ovtsuite.edc16.checksum16.SIZE_KN`` checks for.

Verified against ``03G906016KN.MPC`` / ``03G906016KN_acpacker_patch.MPC``
(Reference/Binaries): both round-trip through :func:`encode`/:func:`decode`
byte-for-byte, and both carry a valid ``ovtsuite.edc16.checksum16`` internal-flash
checksum once decoded.
"""
import zlib

XOR_KEY = 0xD9


def _xor(data: bytes, key: int = XOR_KEY) -> bytes:
    return bytes(b ^ key for b in data)


def is_encoded(data: bytes) -> bool:
    """True if ``data`` looks like an XOR-0xD9'd zlib stream."""
    if len(data) < 2:
        return False
    h0, h1 = data[0] ^ XOR_KEY, data[1] ^ XOR_KEY
    return (h0 & 0x0F) == 8 and ((h0 << 8) | h1) % 31 == 0


def decode(data: bytes):
    """Return ``(plain_image, trailer_bytes)``.

    Raises ``ValueError`` if ``data`` does not look like an XOR-0xD9'd zlib
    stream -- callers that accept either compressed or already-plain images
    should check :func:`is_encoded` first (or catch the raw image directly;
    it will not look encoded).
    """
    if not is_encoded(data):
        raise ValueError(
            "not an XOR-0xD9 zlib MPC (first two bytes de-XOR to %02X %02X)"
            % (data[0] ^ XOR_KEY, data[1] ^ XOR_KEY))
    x = _xor(data)
    d = zlib.decompressobj(15)          # 15 = zlib wrapper, checks Adler-32
    out = d.decompress(x)
    out += d.flush()
    return out, d.unused_data


def encode(image: bytes, trailer: bytes = b"", level: int = 6) -> bytes:
    """Rebuild an MPC file. Pass the original trailer back in if you have it
    (from a prior :func:`decode`) so the output matches the source tool's own
    framing; an empty trailer is fine for a freshly-built file.

    The whole file is one XOR-0xD9 layer over ``zlib_stream || trailer``
    (not just over the zlib stream, with the trailer appended raw after --
    a real file's trailer bytes are XOR'd too, e.g. plain 0xDD is stored as
    0x04, per the module docstring), so the trailer is folded into the same
    XOR pass as the compressed stream, keeping this the exact inverse of
    :func:`decode`."""
    return _xor(zlib.compress(image, level) + trailer)


def decode_auto(data: bytes) -> bytes:
    """Return the plain image whether ``data`` is XOR-0xD9'd zlib or already
    plain. Use this where a file might arrive either way (e.g. a chunk 1
    payload already unwrapped from a .dim, or a loose .MPC someone
    hand-decoded already) and only the plain image is wanted."""
    if is_encoded(data):
        img, _trailer = decode(data)
        return img
    return data

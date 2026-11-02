# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""WinOLS exported projects (``.olsx``): the ``.ols`` inside.

WinOLS 5 exports a project as an "encrypted OLS file". The file is one
buffer, enciphered in place::

    XXTEA-style cipher over all of the file's 32-bit words (little endian,
    the last 1 - 3 bytes of a file whose size is not a multiple of 4 are left
    as they are), 16-byte key
      -> zlib stream (``78 9C``), then the 16-byte MD5 of that stream
      -> an ordinary ``.ols`` (format 597 in the WinOLS 5.87 exports)

The cipher is the XXTEA loop with the 4-bit / 5-bit mix of the first word
only: ``z = v[p-1]``, ``mx = ((z << 4 ^ z >> 5) + z) ^ (key[(p & 3) ^ e] + sum)``,
delta ``0x9E3779B9``, ``6 + 52 / n`` rounds.

WinOLS can bind an export to an EVC customer number and / or a password; such
a file is enciphered with a key made from them and cannot be read here.
Every export made without either (four sample projects: EDC17 Tiguan, Q7,
A6 Allroad and Transporter) uses the one key below, and that is all this
module reads: with any other key the zlib stream does not come out, and
:func:`decrypt` says the file is protected.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from itertools import cycle

from .model import DefinitionError

#: the key of an export bound to neither a customer number nor a password
KEY = bytes.fromhex("7b34238b70846dbc67864850e2a6faea")
_DELTA = 0x9E3779B9
_M = 0xFFFFFFFF


def _decrypt_py(buf: bytes) -> bytes:
    n = len(buf) // 4
    v = list(struct.unpack("<%dI" % n, buf[:n * 4]))
    k = struct.unpack("<4I", KEY)
    s = ((6 + 52 // n) * _DELTA) & _M
    M = _M
    while s:
        e = (s >> 2) & 3
        kc = [(k[q ^ e] + s) & M for q in range(4)]
        # v[p] -= f(v[p - 1]) for p = n-1 .. 1 uses the words as the round
        # began, so all of them can be made from the old list at once
        w = [(a - ((((b << 4) ^ (b >> 5)) + b) ^ c)) & M
             for a, b, c in zip(v[1:], v, cycle((kc[1], kc[2], kc[3], kc[0])))]
        b = w[-1]
        v = [(v[0] - ((((b << 4) ^ (b >> 5)) + b) ^ kc[0])) & M] + w
        s = (s - _DELTA) & M
    return struct.pack("<%dI" % n, *v) + buf[n * 4:]


def _decrypt_np(buf: bytes) -> bytes:
    import numpy as np
    n = len(buf) // 4
    v = np.frombuffer(buf[:n * 4], dtype="<u4").copy()
    key = np.frombuffer(KEY, dtype="<u4")
    s = ((6 + 52 // n) * _DELTA) & _M
    idx = np.arange(1, n, dtype=np.uint32) & np.uint32(3)

    def mix(y, kc):
        return (((y << np.uint32(4)) ^ (y >> np.uint32(5))) + y) ^ kc

    with np.errstate(over="ignore"):
        while s:
            e = (s >> 2) & 3
            kc = key[idx ^ np.uint32(e)] + np.uint32(s)
            v[1:] = v[1:] - mix(v[:-1].copy(), kc)
            y = np.array([v[n - 1]], dtype=np.uint32)
            v[0] = v[0] - mix(y, key[e] + np.uint32(s))[0]
            s = (s - _DELTA) & _M
    return v.tobytes() + buf[n * 4:]


def decipher(buf: bytes) -> bytes:
    """``buf`` deciphered with the default key (the zlib stream and its MD5)."""
    if len(buf) < 24:                   # a zlib stream, its MD5 and a word to spare
        raise DefinitionError("not a WinOLS .olsx file (too short)")
    try:
        return _decrypt_np(buf)
    except ImportError:
        return _decrypt_py(buf)


def decrypt(data: bytes) -> bytes:
    """The ``.ols`` inside a WinOLS ``.olsx`` made without a password or a
    customer number. :class:`DefinitionError` when the file is not an
    ``.olsx`` or is bound to a password / customer number."""
    plain = decipher(bytes(data))
    stream, md5 = plain[:-16], plain[-16:]
    if len(stream) < 2 or stream[0] != 0x78:
        raise _protected()
    try:
        d = zlib.decompressobj()
        out = d.decompress(stream)
    except zlib.error:
        raise _protected() from None
    if not d.eof or d.unused_data:
        raise _protected()
    if hashlib.md5(stream).digest() != md5:
        raise DefinitionError("WinOLS .olsx file is damaged (checksum differs)")
    if out[:16] != b"\x0b\x00\x00\x00WinOLS File\x00":
        raise DefinitionError("WinOLS .olsx file holds no WinOLS project")
    return out


def _protected() -> DefinitionError:
    return DefinitionError(
        "this WinOLS .olsx file is not readable: it is bound to a password or "
        "an EVC customer number (or it is not an .olsx file). Only exports made "
        "without either can be opened here.")

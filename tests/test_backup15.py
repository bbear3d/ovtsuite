# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""KWHack .edc15bak container round-trip + validation."""
import hashlib
import struct
import pytest

from ovtsuite.flash import backup15 as B


def test_roundtrip_and_layout():
    flash = bytes(range(256)) * 4
    ee = bytes(512)
    blob = B.build(flash, ee, "V")
    assert blob[:8] == b"KWHEC15B" and blob[8] == 1 and blob[9] == 1
    assert struct.unpack_from("<i", blob, 10)[0] == len(flash)
    assert blob[-32:] == hashlib.sha256(blob[:-32]).digest()
    f, e, v = B.parse(blob)
    assert f == flash and e == ee and v == "V"


def test_corruption_detected():
    blob = bytearray(B.build(b"\x11" * 100, bytes(512)))
    blob[20] ^= 0xFF
    with pytest.raises(B.BackupError):
        B.parse(bytes(blob))
    with pytest.raises(B.BackupError):
        B.parse(bytes(blob)[:-1])
    with pytest.raises(B.BackupError):
        B.parse(b"NOTABACKUP" * 10)
    with pytest.raises(B.BackupError):
        B.build(b"", b"\x00" * 10)

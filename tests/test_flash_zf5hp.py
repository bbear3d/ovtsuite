# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ZF 5HP experimental K-line flasher: the parts that can be checked without a
TCU -- the SA2 key derivation, the transferData stream, and the hard bench
guard on write_image. The erase/download framing is unverified by design, so
there is nothing to assert about it yet (see ovtsuite/flash/zf5hp.py)."""
import os
import struct

import pytest

from ovtsuite import sgo
from ovtsuite.flash import zf5hp, crypto

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SGO = os.path.join(ROOT, "Reference", "Binaries", "ZF5HP19", "3B0927156AT_2225.sgo")
_TAPE = bytes.fromhex("6805814a058775b57ad5494c")


def test_security_key_is_the_sa2_tape_run_on_the_seed():
    seed = bytes.fromhex("11223344")
    key = zf5hp.security_key(_TAPE, seed)
    assert key == struct.pack(">I", crypto.sa2_execute(_TAPE, 0x11223344) & 0xFFFFFFFF)
    with pytest.raises(zf5hp.Zf5hpError):
        zf5hp.security_key(_TAPE, b"\x00\x00\x00")       # seed must be 4 bytes


@pytest.mark.skipif(not os.path.exists(_SGO), reason="ZF 5HP19 reference .sgo not present")
def test_area_stream_is_the_stored_wire_bytes():
    c = sgo.parse(open(_SGO, "rb").read())
    stream = zf5hp.area_stream(c)
    assert stream == bytes(b ^ 0xFF for b in c.sections[0].blob)
    assert b"BCB Type1" in stream and b"\x1a\x01" in stream


@pytest.mark.skipif(not os.path.exists(_SGO), reason="ZF 5HP19 reference .sgo not present")
def test_write_image_refuses_without_bench_confirmation():
    c = sgo.parse(open(_SGO, "rb").read())
    f = zf5hp.Zf5hpFlash(object(), _TAPE)               # no link call is reached
    with pytest.raises(zf5hp.Zf5hpError):
        f.write_image(c, erase_routine=b"\xC4", download_request=b"\x11")

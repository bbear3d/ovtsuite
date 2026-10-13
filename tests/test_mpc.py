# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ovtsuite.mpc: the XOR-0xD9/zlib framing of .MPC internal-flash images."""

import os
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import mpc  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "03G906016KN.MPC")),
    reason="reference files not present")


def test_is_encoded_false_for_plain_data():
    assert mpc.is_encoded(b"\x00" * 100) is False
    assert mpc.is_encoded(b"") is False
    assert mpc.is_encoded(b"\x01") is False        # too short


def test_is_encoded_true_for_xor_zlib_stream():
    plain = os.urandom(0x1000)
    encoded = mpc.encode(plain)
    assert mpc.is_encoded(encoded) is True


def test_encode_decode_round_trip():
    plain = bytes((i * 7 + 3) % 256 for i in range(0x2000))
    encoded = mpc.encode(plain)
    assert mpc.is_encoded(encoded)
    back, trailer = mpc.decode(encoded)
    assert back == plain
    assert trailer == b""          # encode() with no trailer arg appends none


def test_encode_decode_preserves_trailer():
    plain = os.urandom(0x800)
    trailer = bytes.fromhex("01020304")
    encoded = mpc.encode(plain, trailer=trailer)
    back, got_trailer = mpc.decode(encoded)
    assert back == plain
    assert got_trailer == trailer


def test_decode_rejects_non_encoded_input():
    with pytest.raises(ValueError):
        mpc.decode(b"\x00" * 100)


def test_decode_auto_handles_both_forms():
    plain = os.urandom(0x400)
    assert mpc.decode_auto(plain) == plain             # already plain: pass through
    assert mpc.decode_auto(mpc.encode(plain)) == plain  # compressed: unwrap


def test_encode_uses_plain_zlib_stream():
    """The documented format: XOR(0xD9, zlib_stream) -- de-XOR'ing the first
    two bytes must give a standard zlib header."""
    plain = os.urandom(0x400)
    encoded = mpc.encode(plain)
    h0, h1 = encoded[0] ^ 0xD9, encoded[1] ^ 0xD9
    assert (h0 & 0x0F) == 8               # CM = 8 (deflate)
    assert ((h0 << 8) | h1) % 31 == 0     # zlib header check bits
    dexored = bytes(b ^ 0xD9 for b in encoded)
    assert zlib.decompress(dexored) == plain


@needs_ref
def test_reference_kn_mpc_files_are_plain_and_round_trip():
    """The Reference/Binaries KN.MPC samples are already-decoded 0x81000
    images (not XOR/zlib-compressed) -- confirm that, and that re-encoding
    them through the compressed form and back is lossless."""
    for name in ("03G906016KN.MPC", "03G906016KN_acpacker_patch.MPC"):
        path = os.path.join(BIN, name)
        data = open(path, "rb").read()
        assert len(data) == 0x81000
        assert mpc.is_encoded(data) is False
        encoded = mpc.encode(data)
        assert mpc.is_encoded(encoded)
        back, _trailer = mpc.decode(encoded)
        assert back == data

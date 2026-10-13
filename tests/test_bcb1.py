# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch "BCB Type1" area codec (EDC15 .sgo coding) -- codec round-trips and
the dataset-key solver, against synthetic areas (fast, deterministic) plus
real-file regressions guarded on the EDC15 .sgo corpus being present, the same
pattern tests/test_sgo.py already uses.
"""
import glob
import os

import pytest

from ovtsuite import bcb1
from ovtsuite import sgo

_EDC15_SGO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "Reference", "_incoming")


def _edc15_files():
    out = []
    for pat in ("*.sgo", "*.SGO"):
        for p in glob.glob(os.path.join(_EDC15_SGO, pat)):
            try:
                raw = open(p, "rb").read()
            except OSError:
                continue
            if not sgo.detect(raw):
                continue
            if any(bcb1.looks_like(sgo._xorff(s.blob))
                   for s in sgo.parse(raw).sections):
                out.append(p)
    return sorted(out)


_KEY = bcb1.key_from((0x15, 0x53, 0x63, 0x97))


def _prefix(sum24, name=b"C:\\TEST.CC3 01.01.2000 00:00"):
    return (name + b"   " + (b"%06X" % sum24) + b"\n\r" + bcb1.BANNER +
            b" (C) R.Bosch GmbH'97 K3/EAW2-Wr")


PLAINTEXTS = [
    b"\x00" * 64,                                   # one long run
    bytes(range(256)),                              # no runs at all
    b"\xC3" * 1000 + bytes(range(64)) + b"\xFF" * 7,
    bytes(range(200)) + b"\x5A" * 5000 + b"\x01\x02\x03",
    b"\xAB",                                        # single byte
    b"",                                            # degenerate
]


@pytest.mark.parametrize("plain", PLAINTEXTS)
def test_encode_decode_round_trips(plain):
    body = bcb1.encode(plain, _KEY)
    assert bcb1.decode(body, len(plain), _KEY) == plain


def test_decode_rejects_a_wrong_key():
    plain = bytes(range(256)) * 4
    body = bcb1.encode(plain, _KEY)
    wrong = bcb1.key_from((0x15, 0x53, 0x63, 0x98))       # one byte different
    assert bcb1.decode(body, len(plain), wrong) != plain


def test_decode_rejects_a_corrupted_terminal_checksum():
    plain = b"\x11\x22\x33" * 40
    body = bytearray(bcb1.encode(plain, _KEY))
    body[-1] ^= 0xFF                                   # break the stated sum
    assert bcb1.decode(bytes(body), len(plain), _KEY) is None


def _framable(n):
    """A run-free, narrow-valued plaintext of ``n`` bytes.

    Two properties make an area usable as a constraint, and the real areas
    have both:

    * the record framing must land only on the seven key bytes that are
      constant. For a single all-literal record that means the terminal header
      and its three checksum bytes fall on indices 6..10, i.e. ``n % 11 == 4``.
      An area that needs an unknown byte to frame is skipped by the solver
      rather than guessed (``test_solve_key_refuses_...``).
    * the byte values must be clustered, as calibration data is. The constraint
      is on sums of ``b ^ k``, and XOR against a *uniformly* distributed byte
      set barely moves the sum, so uniform data constrains the key hardly at
      all and the solver drops it (:data:`ovtsuite.bcb1._MAX_CANDIDATES`).
    """
    assert n % 11 == 4 and n <= 0x3FFF
    return bytes((i * 3) % 29 for i in range(n))


def test_solve_key_recovers_the_dataset_key_from_the_areas_alone():
    small = [_framable(1005), _framable(2006)]
    main = b"\x7F" * 4096 + bytes(range(128))
    areas = [(bcb1.encode(p, _KEY), len(p)) for p in small + [main]]
    assert bcb1.solve_key(areas) == _KEY


def test_solve_key_refuses_rather_than_guessing_when_underconstrained():
    # One area leaves the four unknowns with many candidates and nothing
    # independent to confirm against, so the answer must be None -- never a
    # plausible-looking wrong key.
    plain = _framable(1005)
    assert bcb1.solve_key([(bcb1.encode(plain, _KEY), len(plain))]) is None


def test_solve_key_never_returns_a_wrong_key():
    # The invariant that matters: whatever it returns decodes every area.
    small = [_framable(1005), _framable(2006)]
    main = bytes(range(256)) * 9
    areas = [(bcb1.encode(p, _KEY), len(p)) for p in small + [main]]
    got = bcb1.solve_key(areas)
    if got is not None:
        for body, dlen in areas:
            assert bcb1.decode(body, dlen, got) is not None


def test_header_sum_reads_and_writes_the_stated_value():
    pre = _prefix(0xFAC6B6)
    assert bcb1.header_sum(pre) == 0xFAC6B6
    out = bcb1.set_header_sum(pre, 0x012345)
    assert bcb1.header_sum(out) == 0x012345
    assert len(out) == len(pre)
    # the banner line is untouched
    assert out.split(b"\n", 1)[1] == pre.split(b"\n", 1)[1]


def test_header_sum_ignores_the_banner_line():
    # Only the first line states the sum; a 6-char token in the banner must
    # not be mistaken for it.
    pre = b"C:\\X.CC3 01.01.2000 00:00   ABCDEF\n\r" + bcb1.BANNER + b" 123456"
    assert bcb1.header_sum(pre) == 0xABCDEF


def test_looks_like_needs_both_the_banner_and_the_marker():
    assert bcb1.looks_like(_prefix(0) + bcb1.MARKER + b"\x00\x00")
    assert not bcb1.looks_like(b"no banner here" + bcb1.MARKER)
    assert not bcb1.looks_like(_prefix(0) + b"no marker")


@pytest.mark.skipif(not _edc15_files(),
                    reason="EDC15 .sgo corpus not present in Reference/_incoming")
def test_real_edc15_areas_solve_and_validate_against_their_own_checksums():
    for path in _edc15_files():
        raw = open(path, "rb").read()
        container = sgo.parse(raw)
        bodies = []
        for sec in container.sections:
            plain_raw = sgo._xorff(sec.blob)
            assert bcb1.looks_like(plain_raw), path
            bodies.append((bcb1.split(plain_raw)[1], sec.declared_len))
        key = bcb1.solve_key(bodies)
        assert key is not None, path
        assert key[0:2] == b"\x24\xAA" and key[6:] == bytes.fromhex("41E155B23F")
        for sec in container.sections:
            prefix, body = bcb1.split(sgo._xorff(sec.blob))
            plain = (bcb1.decode(body, sec.declared_len, key)
                     or bcb1.decode(body, sec.declared_len, bcb1.KEY_BC4))
            assert plain is not None, (path, sec.index)
            # the sum the area's own provenance header states
            assert (sum(plain) & 0xFFFFFF) == bcb1.header_sum(prefix)


@pytest.mark.skipif(not _edc15_files(),
                    reason="EDC15 .sgo corpus not present in Reference/_incoming")
def test_real_edc15_areas_survive_an_encoder_round_trip():
    path = _edc15_files()[0]
    container = sgo.parse(open(path, "rb").read())
    for sec in container.sections:
        assert sec.scheme == "bcb1"
        again = bcb1.encode(sec.plaintext, sec.bcb1_key)
        assert bcb1.decode(again, sec.declared_len, sec.bcb1_key) == sec.plaintext

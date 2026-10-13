# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Validate the Bosch ESP8 ABS (4F0910517) SecurityAccess key algorithms.

Self-contained vectors -- no hardware or captures needed. Both levels were
recovered from the firmware (handler FUN_00069A86 / key check FUN_000699FA in
the 4F0910517AL_0060 image) and, for the write level, cross-checked against the
VW "SA2" tape in the factory SGO metadata: the tape run through sa2_execute
equals the firmware ROR/XOR formula byte-for-byte for every seed, including the
add-overflow (carry) edge cases that select the other XOR constant.
"""
from ovtsuite.flash.abs_esp8 import (
    program_key, diag_key, ABS_WRITE_TAPE, ABS_READ_CONST,
    ABS_READ_CONST_T2, ABS_READ_CONST_T3,
)
from ovtsuite.flash.crypto import sa2_execute


def _ror32(v, n):
    v &= 0xFFFFFFFF
    n &= 31
    return ((v >> n) | (v << (32 - n))) & 0xFFFFFFFF


def _firmware_write_key(seed):
    """FUN_000699FA(param!=0): t=seed+K; carry-select XOR; ROR 11.
    carry -> 0xFEDCBA98 (boot const @0x7FE0), no-carry -> 0x98765432 (@0x7FE4)."""
    K = 0x974C58AB
    t = (seed + K) & 0xFFFFFFFF
    carry = 1 if (seed + K) > 0xFFFFFFFF else 0
    x = t ^ (0xFEDCBA98 if carry else 0x98765432)
    return _ror32(x, 11)


_SEEDS = [0x12345678, 0x00000000, 0xFFFFFFFF, 0xA5A5A5A5, 0x89ABCDEF,
          0x6A6B0000, 0x974C58AB, 0x1234ABCD, 0xDEADBEEF]


def test_write_tape_equals_firmware_formula_all_seeds():
    for seed in _SEEDS:
        s4 = seed.to_bytes(4, "big")
        assert program_key(s4) == _firmware_write_key(seed).to_bytes(4, "big"), \
            "write key mismatch for seed %08X" % seed


def test_write_key_known_vectors():
    # Produced by both the firmware formula and the SGO tape (they agree).
    assert program_key(bytes.fromhex("1234ABCD")) == bytes.fromhex("09463EEA")
    assert program_key(bytes.fromhex("DEADBEEF")) == bytes.fromhex("A05164D5")
    assert program_key(bytes.fromhex("00010203")) == bytes.fromhex("D381E761")


def test_write_tape_runs_clean():
    # The tape uses only opcodes sa2_execute implements (no ValueError).
    assert sa2_execute(ABS_WRITE_TAPE, 0xDEADBEEF) == 0xA05164D5


def test_read_level_is_additive():
    assert ABS_READ_CONST == 0x2BD7
    assert ABS_READ_CONST_T2 == 0x75FB
    assert ABS_READ_CONST_T3 == 0x9CE8
    assert diag_key(bytes.fromhex("1234ABCD")) == bytes.fromhex("1234D7A4")   # +0x2BD7
    # 32-bit wraparound
    assert diag_key(bytes.fromhex("FFFFFFFF")) == (0xFFFFFFFF + 0x2BD7 & 0xFFFFFFFF).to_bytes(4, "big")


def test_read_tiers():
    seed = bytes.fromhex("00010000")
    base = int.from_bytes(seed, "big")
    assert diag_key(seed, ABS_READ_CONST) == ((base + 0x2BD7) & 0xFFFFFFFF).to_bytes(4, "big")
    assert diag_key(seed, ABS_READ_CONST_T2) == ((base + 0x75FB) & 0xFFFFFFFF).to_bytes(4, "big")
    assert diag_key(seed, ABS_READ_CONST_T3) == ((base + 0x9CE8) & 0xFFFFFFFF).to_bytes(4, "big")

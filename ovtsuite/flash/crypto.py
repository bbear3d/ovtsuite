# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Security-access key algorithms and the EDC16 flash-write cipher.

Ported from KWHack (``Edc15KeyAlgorithms.ComputeLvl41Key`` and
``Edc16FlashVM.Encrypt``), which in turn came from ecu-tool by Javier Vazquez
Vidal. Both are reproduced statement-for-statement.

``encrypt`` is the RC4-like keystream cipher the EDC16 transferData (0x36)
payload must be passed through. Its correctness depends on 32-bit *signed*
arithmetic with sign-extending right shifts (AVR-GCC / C# ``int``), so every
register is wrapped back to a signed 32-bit value after each operation; Python's
``>>`` on a negative int is already arithmetic. The keystream is independent of
the plaintext (EBX is re-seeded from EDX every round), so ``encrypt`` on a zero
block yields the raw keystream -- which is how the port is validated against
MPPS's captured transferData chunks (tests/test_flash_crypto.py).
"""
import zlib
from typing import Optional


def _s32(x: int) -> int:
    """Wrap to a signed 32-bit integer (C# ``int`` semantics)."""
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def compute_lvl41_key(key: int, key3: int, buf) -> bytes:
    """EDC15 LVL41 / EDC16 LVL1 seed->key. ``key`` is the combined 32-bit
    constant (key1<<16 | key2), ``key3`` the magic; ``buf`` the 4 seed bytes.
    Returns the 4 key bytes in wire order."""
    keyread1 = (buf[0] << 8) + buf[1]
    keyread2 = (buf[2] << 8) + buf[3]
    key2 = key & 0xFFFF
    key1 = key >> 16
    for _ in range(5):
        key_temp = keyread1 & 0x8000
        keyread1 <<= 1
        temp1 = key_temp & 0x0FFFF
        if temp1 == 0:
            temp2 = keyread2 & 0xFFFF
            temp3 = key_temp & 0xFFFF0000
            key_temp = temp2 + temp3
            keyread1 &= 0xFFFE
            temp2 = key_temp & 0xFFFF
            temp2 >>= 0x0F
            key_temp &= 0xFFFF0000
            key_temp += temp2
            keyread1 |= key_temp
            keyread2 <<= 1
        else:
            key_temp = keyread2 + keyread2
            keyread1 &= 0xFFFE
            temp2 = key_temp & 0xFF
            temp2 |= 1
            temp3 = key3 & 0xFFFFFF00
            key3 = temp2 + temp3
            key3 &= 0xFFFF00FF
            key3 |= key_temp
            temp2 = keyread2 & 0xFFFF
            temp3 = key_temp & 0xFFFF0000
            key_temp = temp2 + temp3
            temp2 = key_temp & 0xFFFF
            temp2 >>= 0x0F
            key_temp &= 0xFFFF0000
            key_temp += temp2
            key_temp |= keyread1
            key3 ^= key1
            key_temp ^= key2
            keyread2 = key3
            keyread1 = key_temp
    keyread2 &= 0xFFFF
    keyread1 &= 0xFFFF
    return bytes([(keyread1 >> 8) & 0xFF, keyread1 & 0xFF,
                  (keyread2 >> 8) & 0xFF, keyread2 & 0xFF])


# EDC16 (U31/U34) LVL1 flash-write unlock: the shared helper with fixed constants.
EDC16_LVL1_KEY = 0x12890A22
EDC16_LVL1_MAGIC = 0x1C60020


def edc16_lvl1_key(seed4) -> bytes:
    return compute_lvl41_key(EDC16_LVL1_KEY, EDC16_LVL1_MAGIC, seed4)


def edc16_lvl3_key(seed4) -> int:
    """EDC16 LVL3 flash-read unlock: seed + 0x2FC9 (EcuType 1 = U31/U34)."""
    hi = (seed4[0] << 8) + seed4[1]
    lo = (seed4[2] << 8) + seed4[3]
    return (((hi << 16) + lo) + 0x2FC9) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# EDC16 LVL9 (SecurityAccess 27 09 / 27 0A) -- boot/program read+write unlock.
#
# Reversed statically from the 03G906016LQ (P475) binary, NOT from any capture
# (a level-9 seed/key pair is not obtainable). The SecurityAccess handler is a
# table-driven state machine at 0x11F3C. Each level has an 8-byte record
# (config struct @ 0x45C60 -> record table @ 0x45C40):
#
#     rec[2] @0x45C50: subfunc=0x09 grant_bit=1 func_idx=0 arg=0x4C0B0
#
# Key validators live in table B @ 0x4C09C (loaded via -0x7ea4(r13)); entry
# [func_idx=0] = 0x145AC = the level-9 validator. It copies the stored 4-byte
# seed (-0x7eb0(r13)) into a work buffer and runs arg[1]=0x23 (35) rounds of an
# MSB-first LFSR, XORing the polynomial arg[2..5] on carry-out, then compares
# the result to the received key:
#
#     arg struct @0x4C0B0 = 04 23 0A 24 C4 C1 04 01
#       arg[0]=0x04  -> key/seed length = 4 bytes
#       arg[1]=0x23  -> 35 rounds
#       arg[2..5]=0A 24 C4 C1 -> polynomial 0x0A24C4C1
#
# This is a distinct algorithm from LVL1 (SA2 bytecode tape, validator 0x148CC)
# and LVL3 (additive: key-seed matched against an adaptation table, 0x9CBF0) --
# consistent with the rule that no two levels share an algorithm.
#
# Cross-check: captured 27 09 seed 60 35 C9 78 -> key 4C A5 3F BD.

EDC16_LVL9_ROUNDS = 0x23
EDC16_LVL9_POLY = 0x0A24C4C1


def edc16_lvl5_key(seed4, k5) -> int:
    """EDC16 LVL5 unlock: key = (seed + K5) & 0xFFFFFFFF, seed/key 32-bit
    big-endian.  K5 is a 16-bit PER-ECU coding value (RAM 0x7FEA0B, coding struct
    +0x27), read live from the ECU -- NOT a fixed constant (see the protocol doc
    level-5 section).  Same additive shape as level 3, its own (coding) constant."""
    seed = ((seed4[0] << 24) | (seed4[1] << 16) | (seed4[2] << 8) | seed4[3]) & 0xFFFFFFFF
    return (seed + (k5 & 0xFFFF)) & 0xFFFFFFFF


def edc16_lvl9_key(seed4, rounds: int = EDC16_LVL9_ROUNDS,
                   poly: int = EDC16_LVL9_POLY) -> int:
    """EDC16 LVL9 unlock: MSB-first LFSR over the 4-byte seed (big-endian).

    reg = seed; repeat `rounds` times: shift left 1, and if the bit shifted
    out of bit 31 was set, XOR `poly`. Returns the 32-bit key.
    """
    reg = ((seed4[0] << 24) | (seed4[1] << 16) | (seed4[2] << 8) | seed4[3]) & 0xFFFFFFFF
    for _ in range(rounds):
        carry = reg & 0x80000000
        reg = (reg << 1) & 0xFFFFFFFF
        if carry:
            reg ^= poly
    return reg


def zf6hp_read_key(seed4) -> bytes:
    """ZF 6HP TCU security level (requestSeed 0x03 / sendKey 0x04) read unlock:
    seed + 0x11C5A, 32-bit wraparound, big-endian on the wire.

    Not ported from any disassembly -- recovered by cryptanalysis of 4
    independent real seed/key pairs, each from a separate CAN capture of the
    3rd-party flasher tool doing a full read (Reference/Logs/6HP-19A CAN Bus
    Read.log, -read2.log, -read3.log, -read4.log). Every pair reduces to the
    exact same additive constant with zero exceptions:

        3919b510 -> 391ad16a      fc4d945c -> fc4eb0b6
        4146ba00 -> 4147d65a      9b9915be -> 9b9a3218

    Same algorithm *shape* as edc16_lvl3_key above (plain seed + constant, no
    hash/table/XOR) and as the VAG Climatronic 4F/4L primary-unlock -- just a
    6HP-specific constant, confirming they're the same family of VAG unlock
    without literally sharing a constant.

    The WRITE level (requestSeed 0x01 / sendKey 0x02) uses a DIFFERENT
    algorithm -- also solved now, see :func:`zf6hp_write_key` below. It
    turned out not to be a closed-form transform at all (every closed-form
    family was checked and ruled out first) but a small VW "SA2" bytecode
    tape found in the ECU's own raw SGO container; see zf6hp.py's module
    docstring and project memory ``zf6hp-flash-transport`` for the story.
    """
    seed32 = int.from_bytes(bytes(seed4), "big")
    key32 = (seed32 + 0x11C5A) & 0xFFFFFFFF
    return key32.to_bytes(4, "big")


# ---------------------------------------------------------------------------
# ZF 6HP WRITE-level security access: VW "SA2" bytecode
# ---------------------------------------------------------------------------
# VW's real, documented seed/key mechanism used across many post-~2008 VAG
# ECUs: a small 10-opcode bytecode VM (register + carry flag, no lookup
# tables). Per-ECU "tapes" normally ship inside the ECU's FRF/ODX flash
# container. This is an independent port of the documented opcode semantics
# (cross-checked against bri3d/sa2_seed_key, MIT, and jglim/UnlockECU's
# VolkswagenSA2 provider, also MIT) -- no code from any GPL SGO-unpacking
# tool went into this; only the byte LAYOUT of the .sgo container (which is
# not copyrightable expression) was read to find where the tape lives.
#
# Confirmed 2026-09-21: this TCU's WRITE-level tape sits in the clear (no
# XOR, no BCB compression -- unlike the calibration section blobs elsewhere
# in the same file) in the raw .sgo container's own metadata block, right
# before its section table. See project memory ``zf6hp-flash-transport`` for
# the full recovery story and the 18 real seed/key pairs this was verified
# against (18/18 exact matches).

_SA2_OP_RSL = 0x81      # rotate register left 1 bit
_SA2_OP_RSR = 0x82      # rotate register right 1 bit
_SA2_OP_ADD = 0x93      # register += imm32 (big-endian operand); sets carry on overflow
_SA2_OP_SUB = 0x84      # register -= imm32 (big-endian operand); sets carry on borrow
_SA2_OP_EOR = 0x87      # register ^= imm32 (big-endian operand); does NOT touch carry
_SA2_OP_FOR = 0x68      # open a loop: 1-byte count (body repeats count times)
_SA2_OP_NEXT = 0x49     # loop back to the matching FOR's body, or fall through when done
_SA2_OP_BCC = 0x4A      # branch (1-byte skip) if carry flag is CLEAR
_SA2_OP_BRA = 0x6B      # unconditional branch (1-byte skip)
_SA2_OP_FINISH = 0x4C   # no-op; tape ends when the pointer runs off the end


def sa2_execute(tape: bytes, seed32: int) -> int:
    """Run a VW "SA2" seed/key tape against a 32-bit seed; returns the 32-bit
    key. A register/carry-flag machine plus a loop stack for FOR/NEXT -- see
    the module comment above for where tapes come from and the opcode table
    just above this function for what each byte does."""
    reg = seed32 & 0xFFFFFFFF
    carry = 0
    loop_left: list = []
    loop_back: list = []
    ip = 0
    n = len(tape)
    while ip < n:
        op = tape[ip]
        if op == _SA2_OP_RSL:
            carry = (reg >> 31) & 1
            reg = ((reg << 1) | carry) & 0xFFFFFFFF
            ip += 1
        elif op == _SA2_OP_RSR:
            carry = reg & 1
            reg = (reg >> 1) | (carry << 31)
            ip += 1
        elif op == _SA2_OP_ADD:
            operand = int.from_bytes(tape[ip + 1:ip + 5], "big")
            total = reg + operand
            carry = 1 if total > 0xFFFFFFFF else 0
            reg = total & 0xFFFFFFFF
            ip += 5
        elif op == _SA2_OP_SUB:
            operand = int.from_bytes(tape[ip + 1:ip + 5], "big")
            diff = reg - operand
            carry = 1 if diff < 0 else 0
            reg = diff & 0xFFFFFFFF
            ip += 5
        elif op == _SA2_OP_EOR:
            operand = int.from_bytes(tape[ip + 1:ip + 5], "big")
            reg ^= operand
            ip += 5
        elif op == _SA2_OP_FOR:
            count = tape[ip + 1]
            loop_left.append(count - 1)
            ip += 2
            loop_back.append(ip)
        elif op == _SA2_OP_NEXT:
            if loop_left[-1] > 0:
                loop_left[-1] -= 1
                ip = loop_back[-1]
            else:
                loop_left.pop()
                loop_back.pop()
                ip += 1
        elif op == _SA2_OP_BCC:
            skip = tape[ip + 1] + 2
            ip += skip if carry == 0 else 2
        elif op == _SA2_OP_BRA:
            ip += tape[ip + 1] + 2
        elif op == _SA2_OP_FINISH:
            ip += 1
        else:
            raise ValueError("sa2_execute: unknown opcode 0x%02X at tape offset %d" % (op, ip))
    return reg


# The verified WRITE-level tape for the 4F9910156A_0020 (6HP-19A) family --
# found unencrypted in the raw .sgo container's metadata block (file offset
# 0x1B7, 12 bytes long) at C:\Claude\SGOUnpack\6HP Binaries\*.sgo, identical
# across every 4E/4F/4L/8E part number checked except one hardware variant
# (4F5910156S, a different flash-size TCU -- has its own, longer tape; not
# needed here). Verified 18/18 against real captured write seed/key pairs.
ZF6HP_WRITE_TAPE = bytes.fromhex("6806814a05876b5f7dd5494c")


def zf6hp_write_key(seed4) -> bytes:
    """ZF 6HP TCU security level (requestSeed 0x01 / sendKey 0x02) WRITE
    unlock: run :data:`ZF6HP_WRITE_TAPE` through :func:`sa2_execute`.

    Not derived from any disassembly -- recovered from the ECU's own raw SGO
    flash-delivery container, where VW stores it unencrypted (see module
    comment above). Verified against all 18 known real seed/key pairs from
    Reference/Logs/6hp-write*.log (see project memory
    ``zf6hp-flash-transport`` and tests/test_flash_crypto.py)."""
    seed32 = int.from_bytes(bytes(seed4), "big")
    key32 = sa2_execute(ZF6HP_WRITE_TAPE, seed32)
    return key32.to_bytes(4, "big")


# ---------------------------------------------------------------------------
# ZF 6HP transferData (write) cipher: the SGO container's own BCB codec
# ---------------------------------------------------------------------------
# Confirmed 2026-09-21 by direct comparison of a real write capture against a
# real calibration reference bin (not guessed, not assumed from the .sgo
# unpacker's own use of BCB -- checked): the TCU's transferData (SID 0x36)
# payload bytes for one area, concatenated in wire order with the SID byte
# stripped from each frame, ARE byte-for-byte the SAME "BCB" compressed
# stream format the .sgo flash-delivery container's own crypt=1 sections use
# (see SGOUnpack's gs19.map / batch_unpack_6hp.py, read for the container
# byte LAYOUT only -- no GPL code copied here, this is an independent
# reimplementation of the documented record format) -- length/flag-tagged
# literal and RLE records, XORed with a short repeating key -- EXCEPT the
# wire stream skips the container's outer whole-file XOR 0xFF layer: the 2
# -byte marker ``1A 01`` that starts the BCB payload is sitting in the clear
# at the very start of the wire stream, unlike inside a .sgo file where you
# have to undo the outer XOR 0xFF first to see it.
#
# The XOR key is not per-file/cracked -- it is the literal fixed ASCII
# string "MILKYWAY", exactly gs19.map's ``[main] key=MILKYWAY`` (the SAME
# key VW/the flasher tool uses for every 6HP calibration, not something
# unique to one file). Verified by fully decoding BOTH areas of a real write
# capture (Reference/Logs/6hp-write9.log, requestDownload area 0x04 and
# 0x05 segments) and diffing the result against the reference calibration
# (Reference/Logs/4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin):
# area 0x04 decodes to exactly the 65408 bytes at image offset 0x040000,
# area 0x05 to exactly the 196480 bytes at offset 0x050000 -- zero byte
# differences, full length, in both areas. See project memory
# ``zf6hp-flash-transport`` for the recovery story.
#
# One loose end: the format's own terminal "checksum" record (flag 3, a
# 24-bit additive checksum of the decoded bytes per the .sgo unpacker's
# csum()) reads as the fixed value 0x000000 in both real captures rather
# than the actual additive sum of the decoded content -- so
# :func:`zf6hp_bcb_decode` below does NOT verify it (deliberately) and a
# future encoder must not assume "sum of plaintext bytes" is what the real
# tool/TCU actually puts there. Also NOT yet solved: how to build the
# compressed+XORed bytes for NEW calibration content (an encoder) -- decode
# only proves the format, it doesn't by itself prove any particular
# compression choice (literal vs RLE grouping) is what a real TCU firmware
# decoder requires vs. merely permits. Building `encrypt_fn` needs that
# answered first; see zf6hp.py's module docstring.

ZF6HP_BCB_KEY = b"MILKYWAY"
ZF6HP_BCB_MARKER = b"\x1A\x01"


def zf6hp_bcb_decode(stream: bytes, key: bytes = ZF6HP_BCB_KEY) -> bytes:
    """Decode one area's real transferData wire stream back to plaintext
    calibration bytes.

    ``stream`` is the SID 0x36 payloads for one area's write, in wire order,
    with each frame's leading SID byte already stripped and simply
    concatenated (i.e. ``b"".join(msg[1:] for msg in that area's transferData
    messages)``) -- exactly what a fresh :class:`Zf6hpFlash.write_areas` run
    would send, or what you get back out of a real CAN capture reassembled
    via :mod:`ovtsuite.sniff.tp20`.

    Raises :class:`ValueError` if ``stream`` doesn't start with the BCB
    marker or hits a malformed record -- both would mean either a corrupt
    capture or that this isn't actually a BCB-coded area."""
    if not stream.startswith(ZF6HP_BCB_MARKER):
        raise ValueError("zf6hp_bcb_decode: stream does not start with the BCB marker %s "
                          "(got %s)" % (ZF6HP_BCB_MARKER.hex(), stream[:2].hex()))
    body = bytes(b ^ key[i % len(key)] for i, b in enumerate(stream[2:]))
    out = bytearray()
    p, n = 0, len(body)
    while p + 2 <= n:
        header = (body[p] << 8) | body[p + 1]
        p += 2
        flag = header >> 14
        length = header & 0x3FFF
        if flag == 0:          # literal run: `length` raw bytes follow
            out += body[p:p + length]
            p += length
        elif flag == 1:        # RLE run: one byte repeated `length` times
            if p >= n:
                raise ValueError("zf6hp_bcb_decode: truncated RLE run at body offset %d" % p)
            out += bytes([body[p]]) * length
            p += 1
        elif flag == 3:        # terminal record -- checksum bytes follow, NOT verified (see above)
            break
        else:
            raise ValueError("zf6hp_bcb_decode: unknown record flag %d at body offset %d" % (flag, p))
    return bytes(out)


def zf6hp_bcb_additive_checksum(plaintext: bytes) -> int:
    """The 24-bit terminal checksum the 6HP28 UDS/FRF BCB stream carries:
    ``sum(plaintext bytes) & 0xFFFFFF`` (stored big-endian in the terminal
    record, and mirrored in the block's ASCII BCB-Type1 ``[CHECKSUM]`` header).
    Verified against all four blocks (DB_1..DB_4) of a real 8K0927158C 6HP28
    FRF. The older K-line/CAN-TP2.0 6HP family does NOT use this (its real
    captures carry 0x000000); see :func:`zf6hp_bcb_encode`."""
    return sum(plaintext) & 0xFFFFFF


_BCB_MAX_RECORD = 0x3FFF        # 14-bit length field
_BCB_MIN_RLE_RUN = 4             # shorter runs cost more as RLE (2-byte header + 1 byte
                                  # payload) than just folding them into a literal record


def zf6hp_bcb_encode(plaintext: bytes, key: bytes = ZF6HP_BCB_KEY,
                     terminal_checksum: int = 0,
                     min_rle_run: int = _BCB_MIN_RLE_RUN) -> bytes:
    """Encode plaintext calibration bytes for ONE AREA into the real
    transferData wire format: length/flag-tagged literal + RLE records
    (:func:`zf6hp_bcb_decode`'s format), XORed with ``key``, prefixed with
    the cleartext :data:`ZF6HP_BCB_MARKER`. Feed the WHOLE area's plaintext
    in one call -- this is a single continuous compressed stream, not a
    per-254-byte-chunk transform (see zf6hp.py's module docstring for why
    that matters for how ``write_areas`` has to call this).

    ENCODER STATUS -- READ BEFORE USE AGAINST REAL HARDWARE: this round
    -trips through :func:`zf6hp_bcb_decode` (``decode(encode(x)) == x`` for
    every plaintext tried, including both real reference-bin areas -- see
    tests/test_flash_crypto.py) and follows the same record-format rules
    :func:`zf6hp_bcb_decode` enforces, and round-trips through :func:`zf6hp_bcb_decode` for every plaintext tried.
    OEM-EXACT MODE (6HP28 UDS/FRF): with ``min_rle_run=5`` and
    ``terminal_checksum=zf6hp_bcb_additive_checksum(plaintext)`` this
    reproduces VAG's own transferData stream BYTE-FOR-BYTE -- verified
    against all four blocks (DB_1..DB_4) of a real 8K0927158C 6HP28 FRF
    (tests/test_frf.py). The defaults (min_rle_run=4, terminal 0x000000)
    preserve the older K-line/CAN-TP2.0 6HP behaviour; that family's real
    captures carry a 0x000000 terminal, and its exact record split has not
    been pinned down, so for it this remains A valid encoding, not
    necessarily the OEM's. See zf6hp_bcb_decode's docstring and project
    memory ``zf6hp-flash-transport``."""
    out = bytearray()
    i, n = 0, len(plaintext)
    while i < n:
        b = plaintext[i]
        j = i + 1
        while j < n and (j - i) < _BCB_MAX_RECORD and plaintext[j] == b:
            j += 1
        run_len = j - i
        if run_len >= min_rle_run:
            header = (1 << 14) | run_len
            out.append((header >> 8) & 0xFF)
            out.append(header & 0xFF)
            out.append(b)
            i = j
        else:
            k = i
            while k < n and (k - i) < _BCB_MAX_RECORD:
                if (k + min_rle_run <= n
                        and all(plaintext[k] == plaintext[k + t]
                                for t in range(1, min_rle_run))):
                    break
                k += 1
            lit_len = k - i
            header = (0 << 14) | lit_len
            out.append((header >> 8) & 0xFF)
            out.append(header & 0xFF)
            out += plaintext[i:k]
            i = k
    # terminal record: flag=3, length field unused (0), then a 24-bit checksum
    # (big-endian). Default 0x000000 matches the older K-line/CAN-TP2.0 6HP
    # real captures; the 6HP28 UDS/FRF family instead carries the additive
    # byte-sum (sum(plaintext) & 0xFFFFFF) here -- pass
    # ``terminal_checksum=zf6hp_bcb_additive_checksum(plaintext)`` for that
    # family. Verified against all four blocks of a real 6HP28 FRF.
    term_header = (3 << 14)
    out.append((term_header >> 8) & 0xFF)
    out.append(term_header & 0xFF)
    out += (terminal_checksum & 0xFFFFFF).to_bytes(3, "big")

    body = bytes(v ^ key[idx % len(key)] for idx, v in enumerate(out))
    return ZF6HP_BCB_MARKER + body


def zf6hp_area_checksum(plaintext: bytes) -> int:
    """The 16-bit value the TCU's own ``0x31 0xC5`` routine (routineControl,
    local id 0xC5 -- the SAME routine :data:`ROUTINE_VERIFY` in zf6hp.py
    already invokes after every write, and the routine EDC16 calls its
    "checksum" routine, same local id 0xC5) compares against for a given
    address range: ``zlib.crc32(plaintext) & 0xFFFF`` -- the low 16 bits of
    the standard CRC-32 (the one used by zip/PNG/Ethernet, zlib.crc32/
    binascii.crc32), NOT a 16-bit additive sum (EDC16's own algorithm for
    the same-shaped routine -- tried first here, since the request format is
    otherwise identical between the two ECUs, and it does NOT match).

    EVIDENCE (2026-09-21): the routine-0xC5 request bytes captured in every
    real 6HP write session (``Reference/Logs/6hp-write9.log`` and 8 other
    independent write captures, all identical) decode, byte-for-byte, as
    ``0xC5 + start:3B-BE + end:3B-BE + checksum:2B-BE`` -- and the start/end
    fields exactly match ``area*BANK_SIZE`` .. ``area*BANK_SIZE +
    WRITE_AREA_LENGTH[area] - 1`` for both written areas (0x04, 0x05),
    confirming the request shape without guessing it (see zf6hp.py's
    WRITE_AREA_LENGTH docstring for those numbers). That leaves the 2-byte
    checksum field as the only unknown; the REAL plaintext for both areas
    was already available byte-exact (decoded via :func:`zf6hp_bcb_decode`
    and diffed against the reference bin), giving two independent (data,
    expected checksum) pairs to test candidate algorithms against:
    area 0x04 -> 0x4FDC, area 0x05 -> 0xC4C1. A dozen standard 16-bit
    checksum/CRC algorithms were tried against both (EDC16's own additive
    sum, CRC-16 XMODEM/CCITT-FALSE/MODBUS/ARC, Fletcher-16, a 16-bit XOR
    fold, and CRC-32 high/low 16 bits) -- ``zlib.crc32(plaintext) & 0xFFFF``
    was the only one to match, and it matched BOTH areas exactly.

    OPEN (genuinely unconfirmed, needs a bench test, not more log analysis):
    all 9 real write captures write the SAME calibration file, so these are
    still only 2 independent (data, checksum) samples, not many -- a false
    match is astronomically unlikely for a well-known algorithm matching 2
    real values exactly, but it hasn't been cross-checked against a THIRD,
    different calibration. Also unconfirmed: what the routine's response
    looks like for an actual MISMATCH (every real capture only ever
    verifies a write that just succeeded, so "match" is the only response
    shape ever observed) -- callers must not assume the mismatch path is
    exactly EDC16's ``0x73 0xC5 <status>``/``0x7F 0x33 0x40`` shape without
    a bench check."""
    return zlib.crc32(bytes(plaintext)) & 0xFFFF


def encrypt(image, start: int, finish: int):
    """EDC16 transferData cipher over ``image[start:finish]``.

    Returns ``(ciphertext, checksum16)`` where checksum16 is the 16-bit additive
    sum of the PLAINTEXT bytes -- the value the ECU's 0x31 C5 routine compares.
    """
    out = bytearray()
    checksum_acc = 0

    EAX = 0x10000
    ECX = 0x27C0020
    EDX = 0x3FE45D9A
    EBX = 0
    EDI = 0x10000
    EBP = 3

    src = start
    pos = start
    while pos < finish:
        EAX = EDX
        ECX = EDX
        EAX = _s32(EAX >> 20)
        EAX = EAX & 0x400
        ECX = ECX & 0x400
        EAX = EAX ^ ECX
        ECX = EDX
        ECX = _s32(ECX >> 31)
        EAX = _s32(EAX >> 10)
        ECX = ECX & 0x01
        EBX = EDX
        EAX = EAX ^ ECX
        ECX = EDX
        EBX = EBX & 0x01
        ECX = _s32(ECX >> 1)
        EBX = EBX ^ EAX
        if EBX == 0:
            EDI = _s32(EDI & -2)              # & 0xFFFFFFFE
        else:
            EDI = _s32(EDI | 0x01)
        EAX = 0
        EDX = EDI
        EDX = EDX & 0x01
        EDX = EDX | EAX
        if EDX == 0:
            ECX = _s32(ECX & 0x7FFFFFFF)
        else:
            ECX = _s32(ECX | -0x80000000)     # | 0x80000000 (sets the sign bit)
        EBP -= 1
        EDX = ECX

        if EBP == 0:
            EAX = image[src]
            src += 1
            checksum_acc += EAX
            a = (EAX & 0xFF) ^ (ECX & 0xFF)
            out.append(a)
            pos += 1

            c = image[src]
            src += 1
            checksum_acc += c
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + c)
            EAX = ECX
            EAX = _s32(EAX >> 8)
            a = (EAX & 0xFF) ^ (EBX & 0xFF)
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + a)
            EAX = ECX
            a = EBX & 0xFF
            out.append(a)
            pos += 1

            c = image[src]
            src += 1
            checksum_acc += c
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + c)
            EAX = _s32(EAX >> 0x10)
            a = (EAX & 0xFF) ^ (EBX & 0xFF)
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + a)
            EAX = 0x10000
            a = EBX & 0xFF
            out.append(a)
            pos += 1

            c = image[src]
            src += 1
            checksum_acc += c
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + c)
            ECX = _s32(ECX >> 0x18)
            a = (ECX & 0xFF) ^ (EBX & 0xFF)
            EBX = _s32(EBX & -256)
            EBX = _s32(EBX + a)
            EAX -= 1                          # dead w.r.t. output; kept for fidelity
            a = EBX & 0xFF
            out.append(a)
            pos += 1

            EBP = 3

    return bytes(out), checksum_acc & 0xFFFF


def keystream(start: int, finish: int) -> bytes:
    """The raw cipher keystream for [start, finish) (encrypt of a zero image)."""
    zero = bytes(finish)
    return encrypt(zero, start, finish)[0]


# ---------------------------------------------------------------------------
# ZF 8HP (Gen1 "8HPXY", ITCU2, Renesas SH-2A) FRF / transferData codec
# ---------------------------------------------------------------------------
# The 8HP TCU's .frf containers tag every data block ``ENCRYPT-COMPRESS-METHOD
# 22`` (dfi 0x22: compression method 2 / encryption method 2 -- NOT the 6HP's
# 0x11 BCB). Solved 2026-10-09 from 19 real 8K0/8R0927158 FRFs, one of which
# (FL_8R0927158C__YOYU, a development build) is method 00 -- plaintext -- and
# gave the known plaintext the two layers were separated with:
#
#   ENCRYPTION 2 -- a plain repeating XOR with the 19-byte ASCII key
#   ``CyA2008ZFVAGtcuxsam`` starting at stream offset 0. Not a derivation:
#   the key period (19) fell straight out of the ciphertext's own
#   autocorrelation, every key byte is the most frequent ciphertext byte of
#   its column (plaintext 0x00), and the TCU itself carries the literal
#   string in its DB_3 flash-loader block. It is a fixed, per-platform key,
#   the same in all 18 encrypted files (3 hardware indices, 14 software
#   versions).
#
#   COMPRESSION 2 -- a byte-oriented LZSS: a flag byte, then its 8 tokens,
#   MSB first; flag bit 1 = a 2-byte big-endian match token, 0 = one literal
#   byte. Match token: top 5 bits = length (3..31), low 11 bits = distance
#   (1..2047) back from the current output position. No header, no length
#   field, no terminator: the stream ends when the declared UNCOMPRESSED-SIZE
#   has been produced.
#
# Verified: all 72 method-22 blocks across the 18 encrypted files decode to
# exactly their declared UNCOMPRESSED-SIZE AND reproduce the ODX's own CRC32F
# FW-CHECKSUM (zlib.crc32) for every block -- the file's self-declared
# checksum, not a value this code invents.
#
# ENCODER -- OEM-EXACT: :func:`zf8hp_lz_encode` reproduces VAG/ZF's own
# compressed stream BYTE-FOR-BYTE for every block checked (every DB_1..DB_4
# of the corpus, see tests/test_zf8hp.py), which pins down the OEM encoder's
# exact choices, all of which were read off the real streams rather than
# assumed:
#   * greedy, longest match wins, nearest (smallest distance) on ties;
#   * a match never reads bytes it is itself producing: the compared length
#     is capped at the distance (no RLE-style overlap -- "FF FF FF FF" is
#     encoded as three literals then len-3/dist-3, never a dist-1 run);
#   * the OEM compares up to 32 bytes but can only ENCODE 31, so for a long
#     run it picks distance 32 over 31 (raw length 32 beats 31) and then emits
#     length 31 -- the one detail a "natural" implementation gets wrong;
#   * the flag byte is written eagerly at the start of every group, so a
#     token count that is a multiple of 8 ends with a dangling 0x00 flag byte.

ZF8HP_XOR_KEY = b"CyA2008ZFVAGtcuxsam"
ZF8HP_DFI = 0x22                 # ODX ENCRYPT-COMPRESS-METHOD / UDS 0x34 dataFormatIdentifier
_ZF8HP_LZ_MAXLEN = 31            # 5-bit length field
_ZF8HP_LZ_MINLEN = 3
_ZF8HP_LZ_WINDOW = 2047          # 11-bit distance field
_ZF8HP_LZ_CMPLEN = 32            # the OEM's compare limit (one more than it can encode)


def zf8hp_xor(data: bytes, key: bytes = ZF8HP_XOR_KEY, offset: int = 0) -> bytes:
    """Apply (or undo -- it is its own inverse) the 8HP FRF/transferData XOR
    layer. ``offset`` is the stream position of ``data[0]`` so a stream can be
    processed in pieces."""
    klen = len(key)
    return bytes(b ^ key[(i + offset) % klen] for i, b in enumerate(data))


def zf8hp_lz_decode(stream: bytes, expected_len: Optional[int] = None,
                    dist_bits: int = 11) -> bytes:
    """Decompress one 8HP LZSS stream (already XOR- or AES-decoded). With
    ``expected_len`` the decoder stops once that many bytes exist, which is
    how the real stream is delimited (there is no terminator). Raises
    ``ValueError`` on a distance that reaches before the start of the output
    or a token cut off by the end of the stream.

    The BE16 match word splits into a length (top bits, the raw count) and a
    distance (low ``dist_bits``). Gen1 (AL551/AL450/ALX51, method 0x22) uses
    an 11-bit distance / 5-bit length; Gen2 (ALX52, method 0xAA) a 10-bit
    distance / 6-bit length -- the only difference between the two codecs."""
    out = bytearray()
    p, n = 0, len(stream)
    while p < n:
        flag = stream[p]
        p += 1
        for bit in range(7, -1, -1):
            if expected_len is not None and len(out) >= expected_len:
                return bytes(out)
            if p >= n:
                break
            if (flag >> bit) & 1:
                if p + 1 >= n:
                    raise ValueError("zf8hp_lz_decode: match token cut off at %d" % p)
                w = (stream[p] << 8) | stream[p + 1]
                p += 2
                ln, dist = w >> dist_bits, w & ((1 << dist_bits) - 1)
                if dist == 0 or dist > len(out):
                    raise ValueError("zf8hp_lz_decode: bad distance %d at stream %d (out %d)"
                                     % (dist, p - 2, len(out)))
                src = len(out) - dist
                for k in range(ln):
                    out.append(out[src + k])
            else:
                out.append(stream[p])
                p += 1
    if expected_len is not None and len(out) != expected_len:
        raise ValueError("zf8hp_lz_decode: produced %d bytes, expected %d"
                         % (len(out), expected_len))
    return bytes(out)


def zf8hp_lz_encode(plain: bytes, dist_bits: int = 11) -> bytes:
    """Compress ``plain`` into the 8HP LZSS stream, OEM-exactly (see the
    comment block above for the four rules this reproduces). Pure Python,
    roughly 1 MB/min -- fine for a calibration block, slow for a full program
    block; a write path should run it off the GUI thread."""
    n = len(plain)
    head: dict = {}
    toks = []
    i = 0
    # Gen1 is an 11-bit distance / 5-bit length word, Gen2 10/6. The OEM
    # compares one past the longest it can encode (cmplen = maxlen + 1).
    window = (1 << dist_bits) - 1
    maxlen = (1 << (16 - dist_bits)) - 1
    minlen, cmplen = _ZF8HP_LZ_MINLEN, maxlen + 1
    while i < n:
        bl = bd = 0
        if i + 3 <= n:
            cands = head.get(plain[i:i + 3])
            if cands:
                lim = min(cmplen, n - i)
                for pos in reversed(cands):          # most recent (nearest) first
                    dist = i - pos
                    if dist > window:
                        break
                    lm = min(lim, dist)             # no overlap with the output being produced
                    l = 0
                    while l < lm and plain[pos + l] == plain[i + l]:
                        l += 1
                    if l > bl:                      # strictly longer wins; nearest keeps ties
                        bl, bd = l, dist
        if bl >= minlen:
            ln = min(bl, maxlen)
            toks.append((ln << dist_bits) | bd)
            for k in range(ln):
                if i + k + 3 <= n:
                    head.setdefault(plain[i + k:i + k + 3], []).append(i + k)
            i += ln
        else:
            toks.append(-1 - plain[i])              # literal, tagged negative
            if i + 3 <= n:
                head.setdefault(plain[i:i + 3], []).append(i)
            i += 1
    out = bytearray()
    # len//8 + 1 groups: the OEM writes each group's flag byte before it knows
    # whether any token follows, so a token count that is a multiple of 8
    # leaves one dangling 0x00 flag byte at the end (seen on every such block).
    for g in range(0, 8 * (len(toks) // 8 + 1), 8):
        grp = toks[g:g + 8]
        flag = 0
        body = bytearray()
        for k, t in enumerate(grp):
            if t >= 0:
                flag |= 0x80 >> k
                body.append(t >> 8)
                body.append(t & 0xFF)
            else:
                body.append(-1 - t)
        out.append(flag)
        out += body
    return bytes(out)


def zf8hp_frf_decode(raw: bytes, expected_len: int) -> bytes:
    """A whole method-22 FLASHDATA payload (or transferData stream) back to
    plaintext: undo the XOR, then the LZSS."""
    return zf8hp_lz_decode(zf8hp_xor(raw), expected_len)


# --- ZF 8HP Gen2 (ALX52 / "AL552" family, ODX ENCRYPT-COMPRESS-METHOD 0xAA) ---
# The Gen2 controller (MLBevo: Q7/Q8 4M, A6 C8, A4 B9, A8 D5, Bentayga, ...)
# wraps each block in AES-128-CBC and THEN the same LZSS as Gen1 but with a
# 10-bit distance / 6-bit length word. The AES key and IV are not in the FRF;
# they live in the controller's plaintext bootloader and are shared across the
# whole ALX52 family (verified: this one pair decrypts every 4M0/4M8/4ML/4N0/
# 4K0/8W0/80A/760/36A FRF in the corpus, all five blocks' CRC32F matching).
# Harvested from the 4M0 Q7 plaintext reference image (Reference/ZF8HP/A2L
# 4M0AJAM14C3AAH11ZF.hex) at the key/IV words preceding the bootloader tables.
ZF8HP_GEN2_DFI = 0xAA
ZF8HP_GEN2_KEY = bytes.fromhex("f52636339d8fdb0d0627212eac5badf3")
ZF8HP_GEN2_IV = bytes.fromhex("31034202412b523005f49e1108084df8")
ZF8HP_GEN2_DIST_BITS = 10
#: Gen2 (ALX52) WRITE-security SA2 tape -- the <FW-SIGNATURE> SA2 bytecode in
#: every ALX52 FRF (constant across the family). Run through sa2_execute like
#: the 6HP/Gen1 tapes; it is NOT the Gen1 tape, so a Gen2 write needs this one.
ZF8HP_GEN2_WRITE_TAPE = bytes.fromhex(
    "680893231003de4a0b680e814987fa2515786b096804824987bf72d54849845a23f1974c")


def zf8hp_gen2_write_key(seed4, tape: bytes = ZF8HP_GEN2_WRITE_TAPE) -> bytes:
    """ZF 8HP Gen2 write unlock (27 11/12): run the FRF's own SA2 tape through
    :func:`sa2_execute`. Derived from the FRF, not any disassembly.
    UNVERIFIED against a real car (no Gen2 write log yet)."""
    return sa2_execute(tape, int.from_bytes(bytes(seed4), "big")).to_bytes(4, "big")


def zf8hp_gen2_decrypt(cipher: bytes, key: bytes = ZF8HP_GEN2_KEY,
                       iv: bytes = ZF8HP_GEN2_IV) -> bytes:
    """AES-128-CBC decrypt a Gen2 block body. ``cryptography`` is a soft
    dependency used only here (Gen1 never needs it); the error says so."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as ex:                       # pragma: no cover
        raise RuntimeError("ZF 8HP Gen2 (method 0xAA) needs the 'cryptography' "
                           "package for AES-128-CBC: pip install cryptography") from ex
    return Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor().update(cipher)


def zf8hp_gen2_frf_decode(raw: bytes, expected_len: int,
                          key: bytes = ZF8HP_GEN2_KEY, iv: bytes = ZF8HP_GEN2_IV) -> bytes:
    """A whole method-0xAA FLASHDATA payload back to plaintext: AES-128-CBC,
    then the 10-bit-distance LZSS."""
    return zf8hp_lz_decode(zf8hp_gen2_decrypt(raw, key, iv), expected_len,
                           dist_bits=ZF8HP_GEN2_DIST_BITS)


def zf8hp_gen2_encrypt(plain: bytes, key: bytes = ZF8HP_GEN2_KEY,
                       iv: bytes = ZF8HP_GEN2_IV) -> bytes:
    """AES-128-CBC encrypt (inverse of zf8hp_gen2_decrypt); ``plain`` is
    zero-padded to the 16-byte block size the cipher needs."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as ex:                       # pragma: no cover
        raise RuntimeError("ZF 8HP Gen2 (method 0xAA) needs the 'cryptography' "
                           "package for AES-128-CBC: pip install cryptography") from ex
    buf = bytes(plain) + b"\x00" * ((-len(plain)) % 16)
    return Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(buf)


def zf8hp_gen2_frf_encode(plain: bytes, key: bytes = ZF8HP_GEN2_KEY,
                          iv: bytes = ZF8HP_GEN2_IV) -> bytes:
    """Plaintext block -> a method-0xAA FLASHDATA payload: LZSS (10-bit
    distance) then AES-128-CBC. The inverse of zf8hp_gen2_frf_decode. The ECU
    verifies the decompressed plaintext's CRC32, not the compression, so this
    need not reproduce the OEM compressor byte-for-byte -- only decode back to
    the same plaintext (asserted by the round-trip tests)."""
    return zf8hp_gen2_encrypt(zf8hp_lz_encode(plain, dist_bits=ZF8HP_GEN2_DIST_BITS), key, iv)


def zf8hp_frf_encode(plain: bytes) -> bytes:
    """Plaintext to the method-22 wire/FLASHDATA form: LZSS, then XOR."""
    return zf8hp_xor(zf8hp_lz_encode(plain))

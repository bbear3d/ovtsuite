# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Validate the ported EDC16 key algorithms + transferData cipher.

The self-contained vector (the LVL1 seed->key from the MPPS write capture) always
runs. The cipher-vs-capture proof runs only where the KWHack sniffer logs and the
038997016T reference bin are present (the dev machine), and is skipped elsewhere.
"""
import os
import re
import pytest

from ovtsuite.flash.crypto import (edc16_lvl1_key, edc16_lvl3_key, encrypt,
                                    sa2_execute, zf6hp_area_checksum, zf6hp_bcb_decode,
                                    zf6hp_bcb_encode, zf6hp_read_key, zf6hp_write_key)

_KWHACK_LOGS = os.path.expanduser("~/mnt/KWHack/Tools/SnifferLogs")
_REFDIR = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
_LOGDIR = os.path.join(os.path.dirname(__file__), "..", "Reference", "Logs")


def test_lvl1_key_vector():
    # Captured on the wire (docs/EDC16-Write-Protocol-Map.md): seed AC7B56D1 -> key 0FE1AA08.
    assert edc16_lvl1_key(bytes.fromhex("AC7B56D1")) == bytes.fromhex("0FE1AA08")


def test_lvl3_key_is_seed_plus_offset():
    # LVL3 (read unlock) = 32-bit seed + 0x2FC9.
    assert edc16_lvl3_key(bytes.fromhex("00010002")) == (0x00010002 + 0x2FC9)


def test_zf6hp_read_key_matches_all_four_captured_pairs():
    # 4 independent real seed/key pairs from 4 separate CAN captures
    # (Reference/Logs/6HP-19A CAN Bus Read.log, -read2/3/4.log) -- every one
    # of them is seed + 0x11C5A mod 2**32, with zero exceptions. See
    # zf6hp_read_key's docstring / project memory zf6hp-flash-transport for
    # how this was derived (cryptanalysis of the captures, not a guess).
    pairs = [
        ("3919b510", "391ad16a"),
        ("fc4d945c", "fc4eb0b6"),
        ("4146ba00", "4147d65a"),
        ("9b9915be", "9b9a3218"),
    ]
    for seed_hex, key_hex in pairs:
        assert zf6hp_read_key(bytes.fromhex(seed_hex)) == bytes.fromhex(key_hex)


def test_zf6hp_write_key_matches_all_eighteen_captured_pairs():
    # 18 real seed/key pairs from Reference/Logs/6hp-write2/4/5/6/7/8/9.log
    # and 6hp19-write3.log, all TP2.0-reassembly-verified (SID 0x27 sub 0x01
    # request / sub 0x02 response). The algorithm is VW's "SA2" bytecode
    # (see crypto.sa2_execute) -- ZF6HP_WRITE_TAPE was found unencrypted in
    # the raw .sgo container's metadata block, not guessed or brute-forced.
    # See project memory zf6hp-flash-transport for the recovery story.
    pairs = [
        ("fabbfcf3", "dd9aac50"), ("baa35196", "7eda597c"),
        ("95d824fa", "fafcecdc"), ("f31eb25d", "6277fc78"),
        ("a969550d", "4a79f538"), ("535b7f71", "b596faa8"),
        ("a12b1314", "8c5688d0"), ("11494c9e", "9471ad04"),
        ("c8c8e6ef", "6064c908"), ("08525a9e", "c2285c28"),
        ("e9320239", "f9c79454"), ("dc9cc732", "d8114918"),
        ("f3e60a48", "5c59f938"), ("ac180c63", "7d70d36c"),
        ("b41e4731", "6c7e76c0"), ("e167cff1", "3a0a1dfc"),
        ("15d15ee8", "d92a4d50"), ("9efd218d", "e5034ab4"),
    ]
    for seed_hex, key_hex in pairs:
        assert zf6hp_write_key(bytes.fromhex(seed_hex)) == bytes.fromhex(key_hex)


def test_sa2_execute_minimal_tape_matches_read_key():
    # A 2-instruction SA2 tape (ADD 0x11C5A, FINISH) should reproduce the
    # already-solved read-level formula exactly -- confirms sa2_execute's
    # ADD semantics independently of the (longer) write tape above.
    tape = bytes.fromhex("9300011c5a4c")
    for seed in (0x00000000, 0x12345678, 0xDEADBEEF, 0x1A1B1C1D):
        assert sa2_execute(tape, seed) == (seed + 0x11C5A) & 0xFFFFFFFF


def test_zf6hp_bcb_decode_matches_real_write_capture():
    """zf6hp_bcb_decode(real transferData wire bytes) reproduces the
    reference calibration byte-for-byte over both areas a real write
    captured -- proof the transferData cipher IS the .sgo container's own
    BCB codec (key b"MILKYWAY", no extra whole-file XOR 0xFF on the wire).
    See crypto.py's module comment / project memory zf6hp-flash-transport
    for how this was found: by direct comparison of Reference/Logs/6hp-write9.log
    against this reference bin, per Bear's hypothesis that the write cipher
    might be the SGO's own section encryption."""
    log = os.path.join(_LOGDIR, "6hp-write9.log")
    ref_path = os.path.join(_LOGDIR, "4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin")
    if not (os.path.exists(log) and os.path.exists(ref_path)):
        pytest.skip("6hp-write9.log / reference calibration bin not present")

    from ovtsuite.sniff import capture, tp20

    recs = capture.read(log)
    dec = tp20.Tp20Decoder()
    events = []
    for r in recs:
        events.extend(dec.feed(tp20.Frame(r.t, r.can_id, r.data, r.tx)))
    msgs = [e for e in events if e.kind == "message"]

    # Segment transferData (0x36) bursts by the requestDownload (0x34) /
    # requestTransferExit (0x37) that bracket each area, exactly like a real
    # write session -- not a blind concatenation of the whole log.
    segments = []
    cur = None
    for e in msgs:
        p = e.payload
        if not p:
            continue
        if p[0] == 0x34 and len(p) >= 2:
            cur = {"area": p[1], "xfers": []}
        elif p[0] == 0x36 and cur is not None:
            cur["xfers"].append(p[1:])
        elif p[0] == 0x37 and cur is not None:
            segments.append(cur)
            cur = None

    area_offsets = {0x04: 0x040000, 0x05: 0x050000}
    assert segments, "no requestDownload/transferData/requestTransferExit segments found"
    ref = open(ref_path, "rb").read()
    checked = 0
    for seg in segments:
        off = area_offsets.get(seg["area"])
        if off is None:
            continue
        stream = b"".join(seg["xfers"])
        decoded = zf6hp_bcb_decode(stream)
        assert decoded == ref[off:off + len(decoded)]
        checked += 1
    assert checked == 2, "expected to check both written areas (0x04, 0x05)"


def test_zf6hp_bcb_encode_round_trips_arbitrary_data():
    """zf6hp_bcb_encode -> zf6hp_bcb_decode must reproduce the original bytes
    exactly, across edge cases (empty, short runs on either side of the RLE
    threshold, a run past the 14-bit record-length boundary, high-entropy
    data). This is the encoder's own correctness bar (self-consistency with
    our decoder) -- it does NOT by itself prove pcmflash's real compression
    choices are matched; see crypto.py's module comment above
    zf6hp_bcb_encode for what that would take before trusting this against
    real hardware."""
    cases = [
        b"",
        b"A",
        b"AAA",                                     # below the RLE threshold (4)
        b"AAAA",                                     # right at the RLE threshold
        bytes([0xFF]) * 20000,                        # run > 0x3FFF, forces multi-record RLE
        bytes(range(256)) * 100,
        os.urandom(9000),
        bytes([0]) * 5000 + os.urandom(3000) + bytes([0xAB]) * 40000,
    ]
    for plain in cases:
        enc = zf6hp_bcb_encode(plain)
        assert zf6hp_bcb_decode(enc) == plain, "round-trip failed for %d-byte case" % len(plain)


def test_zf6hp_bcb_encode_round_trips_real_reference_areas():
    """Same round-trip proof, but against the two REAL calibration areas from
    the reference bin (the same content zf6hp_bcb_decode's real-capture test
    above decodes from the wire) -- confirms the encoder handles genuine
    calibration data, not just synthetic edge cases."""
    ref_path = os.path.join(_LOGDIR, "4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin")
    if not os.path.exists(ref_path):
        pytest.skip("reference calibration bin not present")
    ref = open(ref_path, "rb").read()
    area4 = ref[0x040000:0x040000 + 65408]
    area5 = ref[0x050000:0x050000 + 196480]
    for plain in (area4, area5):
        enc = zf6hp_bcb_encode(plain)
        assert zf6hp_bcb_decode(enc) == plain


def test_zf6hp_area_checksum_matches_both_real_captured_routine_0xc5_values():
    """zf6hp_area_checksum(plaintext) == zlib.crc32(plaintext) & 0xFFFF must
    reproduce the checksum field the TCU's own routine-0xC5 (checksum/verify)
    request carries for a real write, for BOTH written areas.

    EVIDENCE: routine-0xC5's request bytes are identical across all 9 real
    write captures on file (Reference/Logs/6HP-19A CAN Bus Write.log,
    6hp-write2/4/5/6/7/8/9.log, 6hp19-write3.log -- all the same calibration)
    and decode as ``0xC5 + start:3B-BE + end:3B-BE + checksum:2B-BE``; the
    start/end fields exactly match area*BANK_SIZE .. area*BANK_SIZE +
    WRITE_AREA_LENGTH[area] - 1 for both areas, confirming the request shape
    without guessing it. That leaves the checksum field (area 0x04 ->
    0x4FDC, area 0x05 -> 0xC4C1) as the only unknown, tested here against
    the SAME real plaintext zf6hp_bcb_decode's real-capture test above
    proves byte-exact -- a dozen standard 16-bit checksum/CRC algorithms
    were tried against both real values (see zf6hp_area_checksum's
    docstring for the list); CRC-32 low-16 was the only one to match, and it
    matched both. Still only 2 independent samples (every real capture
    writes the same file) -- see that docstring's OPEN section before
    trusting this against a bench write of a DIFFERENT calibration."""
    ref_path = os.path.join(_LOGDIR, "4F9910156A_0020_AG6_09L_3,0TFSI_RdW_maps-sniffed.bin")
    if not os.path.exists(ref_path):
        pytest.skip("reference calibration bin not present")
    ref = open(ref_path, "rb").read()
    area4 = ref[0x040000:0x040000 + 65408]
    area5 = ref[0x050000:0x050000 + 196480]
    assert zf6hp_area_checksum(area4) == 0x4FDC
    assert zf6hp_area_checksum(area5) == 0xC4C1


def test_encrypt_checksum_is_plaintext_sum():
    data = bytes(range(256)) * 4        # 1024 bytes, divisible by 4
    _, cks = encrypt(data, 0, len(data))
    assert cks == sum(data) & 0xFFFF


def _load_capture_stream():
    logs = [os.path.join(_KWHACK_LOGS, n)
            for n in ("edc16mppswriteslow.log", "edc16mppswriteslow38400.log")]
    if not all(os.path.exists(p) for p in logs):
        pytest.skip("KWHack MPPS write captures not present")
    pat = re.compile(r"bytes:\s+([0-9A-Fa-f]{2}(?:\s+[0-9A-Fa-f]{2})*)")
    s = bytearray()
    for lg in logs:
        for line in open(lg, errors="ignore"):
            m = pat.search(line)
            if m:
                s.extend(bytes.fromhex(m.group(1).replace(" ", "")))
    return bytes(s)


def test_cipher_matches_mpps_ciphertext():
    """encrypt(reference plaintext) reproduces MPPS's real on-the-wire ciphertext
    byte-for-byte over the region where the written image equals the T-bin."""
    tbin = os.path.join(_REFDIR, "038997016T.Bin")
    if not os.path.exists(tbin):
        pytest.skip("038997016T reference bin not present")
    stream = _load_capture_stream()
    start = stream.find(bytes.fromhex("341C00000203E000"))   # requestDownload block7
    assert start >= 0, "requestDownload(block7) not found in capture"
    chunks, i = [], start
    while len(chunks) < 3:
        j = stream.find(bytes.fromhex("00F936"), i)
        if j < 0:
            break
        chunks.append(stream[j + 3:j + 3 + 248])
        i = j + 3 + 248
    captured = b"".join(chunks)
    T = open(tbin, "rb").read()
    ct, _ = encrypt(T, 0x1C0000, 0x1C0000 + len(captured))
    # The first 702 bytes are where MPPS's written image equals the T-bin; the
    # cipher must reproduce those exactly (proof it's the true transform).
    assert ct[:700] == captured[:700]


def test_zf6hp_bcb_additive_terminal_checksum():
    """The 6HP28 UDS/FRF BCB terminal record carries sum(plaintext)&0xFFFFFF
    (big-endian); the older family uses 0x000000. The encoder must default to
    the old behaviour and emit the additive value only when asked."""
    from ovtsuite.flash.crypto import (zf6hp_bcb_encode, zf6hp_bcb_decode,
                                        zf6hp_bcb_additive_checksum,
                                        ZF6HP_BCB_KEY, ZF6HP_BCB_MARKER)

    def terminal(stream):
        body = bytes(x ^ ZF6HP_BCB_KEY[i % len(ZF6HP_BCB_KEY)]
                     for i, x in enumerate(stream[len(ZF6HP_BCB_MARKER):]))
        p = 0
        while p + 2 <= len(body):
            h = (body[p] << 8) | body[p + 1]; p += 2
            flag, ln = h >> 14, h & 0x3FFF
            if flag == 0: p += ln
            elif flag == 1: p += 1
            elif flag == 3: return body[p:p + 3]
            else: return None
        return None

    pt = bytes(range(256)) * 5 + b"\xFF" * 1500 + bytes((i * 7) & 0xFF for i in range(300))
    assert zf6hp_bcb_additive_checksum(pt) == sum(pt) & 0xFFFFFF
    # default: terminal is zero, and round-trips
    enc0 = zf6hp_bcb_encode(pt)
    assert terminal(enc0) == b"\x00\x00\x00"
    assert zf6hp_bcb_decode(enc0) == pt
    # 6HP28: terminal carries the additive checksum, big-endian, and round-trips
    enc1 = zf6hp_bcb_encode(pt, terminal_checksum=zf6hp_bcb_additive_checksum(pt))
    assert int.from_bytes(terminal(enc1), "big") == zf6hp_bcb_additive_checksum(pt)
    assert zf6hp_bcb_decode(enc1) == pt

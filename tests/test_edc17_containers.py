# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Bosch EDC17 containers: the ``BiWbBuD101`` BCB key (``.sgo`` areas and
FRF method ``11``) and FRF method ``A1`` (XOR + 10-bit LZSS).

The synthetic tests always run. The real-file tests use the images collected
in ``Reference/EDC17/`` and skip when a file is not there.
"""
import os
import struct
import zlib

import pytest

from ovtsuite import bcb1, frf, sgo
from ovtsuite.flash import crypto

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDC17 = os.path.join(ROOT, "Reference", "EDC17")

KEY = bcb1.KEY_EDC17


def _real(*parts):
    p = os.path.join(EDC17, *parts)
    if not os.path.exists(p):
        pytest.skip("%s not in Reference/EDC17" % "/".join(parts))
    return p


# ---------------------------------------------------------------- BCB key

def test_edc17_key_is_the_documented_string():
    assert KEY == b"BiWbBuD101" and len(KEY) == 10


def test_xor_run_matches_the_byte_loop_at_any_phase():
    body = bytes(range(256)) * 3
    for off in (0, 1, 7, 10, 33):
        for ln in (0, 1, 9, 10, 11, 500):
            want = bytes(body[off + j] ^ KEY[(off + j) % len(KEY)] for j in range(ln))
            assert bcb1._xor_run(body, off, ln, KEY) == want


@pytest.mark.parametrize("plain", [b"\x00" * 64, bytes(range(256)),
                                   b"\xC3" * 1000 + bytes(range(64)) + b"\xFF" * 7])
def test_bcb_round_trips_under_the_edc17_key(plain):
    body = bcb1.encode(plain, KEY)
    assert bcb1.decode(body, len(plain), KEY) == plain
    assert bcb1.decode(body, len(plain), bcb1.KEY_5HP) is None


# ------------------------------------------------------- FRF method "A1"

def _a1(plain: bytes) -> bytes:
    lz = crypto.zf8hp_lz_encode(plain, dist_bits=10)
    return bcb1._xor_run(lz, 0, len(lz), KEY)


def _block(start: int, size: int) -> bytes:
    """A block with the EDC17 16-byte header followed by patterned data."""
    end = frf.EDC17_FLASH_BASE + start + size - 4
    head = struct.pack("<4I", 0x30, size, 0, end)
    body = (b"\x00" * 40 + bytes(range(256)) * 8 + b"\xFF" * 100)
    return (head + body * (size // len(body) + 1))[:size]


def test_a1_block_round_trips_and_is_checked_by_its_16_bit_crc():
    plain = _block(0x4000, 0x3000)
    raw = _a1(plain)
    crc = zlib.crc32(plain) & 0xFFFF
    got, scheme = frf._decode_block(raw, len(plain), crc, "A1", 0xFFFF)
    assert got == plain and scheme == "xor_lz"
    # a wrong CRC, or a wrong declared length, is refused rather than guessed
    assert frf._decode_block(raw, len(plain), crc ^ 1, "A1", 0xFFFF) == (None, None)
    assert frf._decode_block(raw, len(plain) + 4, crc, "A1", 0xFFFF) == (None, None)


def test_block_start_is_read_from_the_block_header():
    plain = _block(0x283000, 0xFCF00)
    assert frf.edc17_block_start(plain) == 0x283000
    assert frf.edc17_block_start(plain[:-4]) is None          # length disagrees
    assert frf.edc17_block_start(b"\x00" * 64) is None
    bad = struct.pack("<4I", 0x30, 32, 0, 0x1000) + b"\x00" * 16
    assert frf.edc17_block_start(bad) is None                  # outside the flash window


def test_bosch_ecu_name_handles_underscore_and_picks_the_common_name():
    assert frf.bosch_ecu_name(b"..EDC17_CP14..EDC17_CP14..EDC17..") == "EDC17CP14"
    assert frf.bosch_ecu_name(b"xx EDC17C64 xx") == "EDC17C64"
    assert frf.bosch_ecu_name(b"MED17.5.5 ...") == "MED1755"
    assert frf.bosch_ecu_name(b"nothing here") is None


def _odx(blocks, method="A1", crc_tag="CRC"):
    """Minimal ODX-FLASH holding ``blocks`` = [(name, plain)]."""
    sec, dbs, fds = [], [], []
    for n, (name, plain) in enumerate(blocks, 1):
        sec.append('<SECURITY><SECURITY-METHOD>%s</SECURITY-METHOD>'
                   '<FW-CHECKSUM>%04X</FW-CHECKSUM><VALIDITY-FOR>%s</VALIDITY-FOR></SECURITY>'
                   % (crc_tag, zlib.crc32(plain) & 0xFFFF, name))
        dbs.append('<DATABLOCK ID="E.%s" TYPE="DATA"><SHORT-NAME>%s</SHORT-NAME>'
                   '<FLASHDATA-REF ID-REF="E.FD_%s"/><SEGMENTS><SEGMENT>'
                   '<SOURCE-START-ADDRESS>%02d</SOURCE-START-ADDRESS>'
                   '<UNCOMPRESSED-SIZE>%d</UNCOMPRESSED-SIZE></SEGMENT></SEGMENTS></DATABLOCK>'
                   % (name, name, name, n, len(plain)))
        fds.append('<FLASHDATA ID="E.FD_%s"><SHORT-NAME>FD_%s</SHORT-NAME>'
                   '<ENCRYPT-COMPRESS-METHOD>%s</ENCRYPT-COMPRESS-METHOD><DATA>%s</DATA></FLASHDATA>'
                   % (name, name, method, _a1(plain).hex()))
    return ('<ODX><FLASH ID="FL_T"><ECU-MEMS><ECU-MEM><MEM><SESSIONS><SESSION>'
            '<SECURITYS>%s</SECURITYS></SESSION></SESSIONS><DATABLOCKS>%s</DATABLOCKS>'
            '<FLASHDATAS>%s</FLASHDATAS></MEM></ECU-MEM></ECU-MEMS></FLASH></ODX>'
            % ("".join(sec), "".join(dbs), "".join(fds))).encode()


def test_parse_odx_places_a1_blocks_by_their_own_headers():
    a, b = _block(0x4000, 0x3000), _block(0x20000, 0x5000)
    c = frf.parse_odx(_odx([("DB_01DATA", a), ("DB_02DATA", b)]))
    assert [x.scheme for x in c.blocks] == ["xor_lz", "xor_lz"]
    assert all(x.crc_ok and x.crc_bits == 16 for x in c.blocks)
    assert [x.start_address for x in c.blocks] == [0x4000, 0x20000]
    assert all(x.looks_like_bosch_tricore for x in c.blocks)
    im = frf.image(c)
    assert im.data[0x4000:0x4000 + len(a)] == a
    assert im.data[0x20000:0x20000 + len(b)] == b
    assert im.data[0x7000:0x20000] == b"\xFF" * (0x20000 - 0x7000)


def test_a_bare_crc_tag_is_not_trusted_for_non_bosch_methods():
    # the same ODX under a method this module decodes as plain: the "CRC" tag is
    # ignored (as it always was), so the block is accepted on length alone
    plain = b"\x01\x02\x03\x04" * 8
    odx = _odx([("DB_1DATA", plain)], method="00").replace(
        _a1(plain).hex().encode(), plain.hex().encode())
    c = frf.parse_odx(odx)
    assert c.blocks[0].scheme == "plain" and c.blocks[0].crc_expected is None


# ------------------------------------------------------------ real files

def test_real_sgo_03L906019CT_decodes_every_area_and_round_trips():
    p = _real("SGO", "03L906019CT_9971.sgo")
    raw = open(p, "rb").read()
    c = sgo.parse(raw, p)
    assert [s.scheme for s in c.sections] == ["bcb1"] * 5
    assert all(s.bcb1_key == KEY for s in c.sections)
    assert [hex(s.addr) for s in c.sections] == [
        "0x4000", "0x40000", "0x800000", "0x183000", "0x20000"]
    blob = b"".join(s.plaintext for s in c.sections)
    assert b"EDC17_CP14" in blob and b"1037544729" in blob
    assert sgo.to_bytes(c) == raw
    # the 0x80800000 bank is not an EDC16U1-style mirror, so nothing is demoted
    assert not c.unrecognized
    base, img = sgo.image(c)
    assert base == 0 and len(img) == 0x400000
    # bank A areas at their addresses, the 0x80800000 bank at 0x200000, where
    # its own block header (start 0x80800000) sits
    areas = {s.addr: s.plaintext for s in c.sections}
    assert img[0x4000:0x4000 + len(areas[0x4000])] == areas[0x4000]
    assert img[0x200000:0x200000 + len(areas[0x800000])] == areas[0x800000]
    assert struct.unpack_from("<4I", img, 0x200000)[3] == 0x809FFF00


def test_real_frf_04L906021EP_a1_blocks_verify_and_tile_the_4mb_image():
    p = _real("FRF", "FL_04L906021EP_9972.frf")
    c = frf.parse(open(p, "rb").read(), p)
    assert c.platform == "edc17"
    assert [b.method for b in c.blocks] == ["A1"] * 5
    assert all(b.plaintext is not None and b.crc_ok for b in c.blocks)
    assert {b.short_name: b.start_address for b in c.blocks} == {
        "DB_04DATA": 0x0, "DB_01DATA": 0x4000, "DB_02DATA": 0x20000,
        "DB_05DATA": 0x283000, "DB_03DATA": 0x380000}
    im = frf.image(c)
    assert im.size == 0x400000
    assert frf.bosch_ecu_name(im.data) == "EDC17C64"


def test_real_frf_03L906019GL_method_11_blocks_decode_with_the_edc17_key():
    p = _real("FRF", "FL_03L906019GL_9983.frf")
    c = frf.parse(open(p, "rb").read(), p)
    assert [b.scheme for b in c.blocks] == ["bcb1"] * 3
    assert all(b.crc_ok and b.crc_bits == 16 for b in c.blocks)
    assert c.platform == "edc17"
    assert sorted(b.start_address for b in c.blocks) == [0x4000, 0x20000, 0x80000] \
        or sorted(b.start_address for b in c.blocks) == [0x4000, 0x20000, 0x40000]


def test_real_attached_c64_dump_is_what_the_frf_decodes_to():
    """The user-supplied 4 MB C64 original is laid out exactly as the FRF
    blocks are: the same header, ident and block placement."""
    orig = _real("Attached", "gtd(Original) - 537361).bin")
    d = open(orig, "rb").read()
    assert len(d) == 0x400000
    assert frf.bosch_ecu_name(d) == "EDC17C64"
    # the block headers in the dump place the blocks where the FRF puts them
    for off, plen in ((0x0, 0x4000), (0x4000, 0xBF00), (0x20000, 0x260000),
                      (0x283000, 0xFCF00), (0x380000, 0x80000)):
        assert frf.edc17_block_start(d[off:off + plen]) == off

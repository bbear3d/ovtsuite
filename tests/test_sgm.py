# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""VW SW-CNT (``.sgm``) containers: synthetic documents always, the three real
EDC17 containers in ``Reference/EDC17/DAMOS`` when they are there."""
import base64
import glob
import os
import struct
import zlib

import pytest

from ovtsuite import bcb1, sgm, sgo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAMOS = os.path.join(ROOT, "Reference", "EDC17", "DAMOS")


def _area(plain: bytes, start: int) -> bytes:
    """A BCB area as a ``.sgm`` carries it: provenance header, ``1A 01``, then
    the records under the EDC17 key."""
    prefix = (b"[SOURCE]\nMaster.hex 01.01.2020 00:00:00\n[CHECKSUM]\n%06X\n[FORMAT]\n"
              b"BCB Type1 (C) R. Bosch GmbH 2000 K3/EMW3-Ws\n[START_ADDRESS]\n%08X\n"
              b"[END_ADDRESS]\n%08X\n" % (sum(plain) & 0xFFFFFF, 0x80000000 + start,
                                          0x80000000 + start + len(plain) - 1))
    return prefix + b"\x1a\x01" + bcb1.encode(plain, bcb1.KEY_EDC17)


def _sgm(blocks, tamper=None, bad_b64=False, extra=""):
    """``blocks`` = [(start, plain)]. ``tamper`` flips one byte of block 0's
    stream; ``bad_b64`` corrupts block 0's base64."""
    out = ['<!DOCTYPE SW-CNT PUBLIC "-//Volkswagen AG//DTD Datencontainer//GE" "x.dtd">%s'
           '<SW-CNT><IDENT><CNT-DATEI>T_1.sgm</CNT-DATEI><CNT-VERSION-TYP>cvt_pfu_01</CNT-VERSION-TYP>'
           '<CNT-VERSION-INHALT>1.01</CNT-VERSION-INHALT><CNT-IDENT-TEXT>\n-\n'
           'Kundennummer\t03L906022XX\nBosch SW-TTNR\t-\nBosch HW-TTNR\t-\n</CNT-IDENT-TEXT>'
           '<SW-VERSION-KURZ>1</SW-VERSION-KURZ><SW-VERSION-LANG>03L906022XX_1</SW-VERSION-LANG></IDENT>'
           '<ABLAEUFE><ABLAUF><ABLAUF-NAME>abn_pfu</ABLAUF-NAME><KWP-2000><BAUDRATEN-2000>'
           '<BAUDRATE-2000><BAUDRATE-TESTER>10,4</BAUDRATE-TESTER><BAUDRATE-SG>0x14</BAUDRATE-SG>'
           '</BAUDRATE-2000></BAUDRATEN-2000><KWP-2000-TGT>0x01</KWP-2000-TGT>'
           '<KWP-2000-ACP><KWP-2000-P2MIN>0x00</KWP-2000-P2MIN><KWP-2000-P2MAX>0x02</KWP-2000-P2MAX>'
           '</KWP-2000-ACP><KWP-2000-SA2>0x68,0x05,0x81,0x4A</KWP-2000-SA2></KWP-2000>'
           '<DATEN-VERWEISE>%s</DATEN-VERWEISE></ABLAUF></ABLAEUFE><DATEN><DATENBLOECKE>'
           % (extra, "".join("<DATEN-VERWEIS>dav_%d</DATEN-VERWEIS>" % i
                             for i in range(len(blocks))))]
    for i, (start, plain) in enumerate(blocks):
        raw = _area(plain, start)
        if tamper is not None and i == 0:
            b = bytearray(raw)
            b[-8] ^= 0x55
            raw = bytes(b)
        b64 = base64.b64encode(raw).decode()
        if bad_b64 and i == 0:
            b64 = "!!" + b64[2:]
        lines = "\n".join(b64[j:j + 72] for j in range(0, len(b64), 72))
        end = start + len(plain) - 1
        out.append(
            '<DATENBLOCK><DATENBLOCK-NAME>dav_%d</DATENBLOCK-NAME>'
            '<DATENBLOCK-FORMAT-NAME>dfn_mime</DATENBLOCK-FORMAT-NAME>'
            '<START-ADR>0x%06X</START-ADR><DATENBLOCK-FORMAT>0x11</DATENBLOCK-FORMAT>'
            '<GROESSE-DEKOMPRIMIERT>0x%X</GROESSE-DEKOMPRIMIERT>'
            '<LOESCH-BEREICH><START-ADR>0x%06X</START-ADR><END-ADR>0x%06X</END-ADR></LOESCH-BEREICH>'
            '<DATENBLOCK-CHECK><START-ADR>0x%06X</START-ADR><END-ADR>0x%06X</END-ADR>'
            '<CHECKSUMME>0x%04X</CHECKSUMME></DATENBLOCK-CHECK><DATENBLOCK-DATEN>\n'
            'Mime-Version: 1.0\nContent-Type: application/octet-stream; name="T_1.bc%d"\n'
            'Content-Transfer-Encoding: base64\n\n%s\n</DATENBLOCK-DATEN></DATENBLOCK>'
            % (i, start, len(plain), start, end, start, end, zlib.crc32(plain) & 0xFFFF,
               i + 1, lines))
    out.append("</DATENBLOECKE></DATEN></SW-CNT>")
    return "".join(out).encode("latin-1")


A = bytes(range(256)) * 12 + b"\xFF" * 40
B = (b"\x00" * 300 + bytes(range(200)) + b"\xC3" * 900)[:1200]


def test_detect():
    assert sgm.detect(_sgm([(0x4000, A)]))
    assert not sgm.detect(b"SGML Object File" + bytes(100))
    assert not sgm.detect(b"<html><body>SW</body></html>")


def test_synthetic_blocks_decode_to_the_bytes_that_went_in():
    c = sgm.parse(_sgm([(0x4000, A), (0x800000, B)]), "t.sgm")
    assert [s.scheme for s in c.sections] == ["bcb1", "bcb1"]
    assert [s.plaintext for s in c.sections] == [A, B]
    assert not c.unrecognized
    assert all(s.bcb1_key == bcb1.KEY_EDC17 for s in c.sections)
    assert c.part_number == "03L906022XX" and c.software == "1"
    assert c.sa2_tape == bytes([0x68, 0x05, 0x81, 0x4A])
    assert c.flash_order == ["dav_0", "dav_1"]
    assert c.kwp["baud"] == [("10,4", 0x14)] and c.kwp["target"] == 1
    assert c.kwp["timing"] == {"P2MIN": 0, "P2MAX": 2}
    assert [(b.name, b.start, b.fmt, b.declared_len) for b in c.blocks] == [
        ("dav_0", 0x4000, 0x11, len(A)), ("dav_1", 0x800000, 0x11, len(B))]
    assert c.blocks[0].erase == (0x4000, 0x4000 + len(A) - 1)
    assert c.blocks[0].content_name == "T_1.bc1"


def test_image_places_the_second_bank_at_0x200000():
    c = sgm.parse(_sgm([(0x4000, A), (0x800000, B)]))
    base, img = sgm.image(c)
    assert base == 0 and len(img) == 0x400000
    assert img[0x4000:0x4000 + len(A)] == A
    assert img[0x200000:0x200000 + len(B)] == B
    assert img[0x4000 + len(A):0x200000] == b"\xFF" * (0x200000 - 0x4000 - len(A))


def test_a_block_that_fails_the_formats_own_checks_is_left_unrecognized():
    c = sgm.parse(_sgm([(0x4000, A), (0x20000, B)], tamper=True))
    assert [s.scheme for s in c.sections] == [None, "bcb1"]
    assert c.sections[0].plaintext is None and c.sections[1].plaintext == B
    assert len(c.unrecognized) == 1


def test_unusable_base64_is_reported_not_raised():
    c = sgm.parse(_sgm([(0x4000, A), (0x20000, B)], bad_b64=True))
    assert c.blocks[0].problem and not c.blocks[1].problem
    assert [s.addr for s in c.sections] == [0x20000]


def test_entities_and_garbage_are_refused():
    with pytest.raises(ValueError):
        sgm.parse(_sgm([(0x4000, A)], extra='<!ENTITY x "y">'))
    with pytest.raises(ValueError):
        sgm.parse(b"<SW-CNT><IDENT>")
    with pytest.raises(ValueError):
        sgm.parse(b"not xml at all")


def test_section_header_is_the_sgo_section_table_entry():
    h = sgm._header(0x4000, 0x11, 0xBD04, (0x4000, 0xFFFF), (0x4000, 0xFD03), 0x4797, 0xB9C6)
    assert h.hex() == "0040001100bd0400400000ffff00400000fd034797c6b90000"   # 03L906019CT's, from its .sgo


# ------------------------------------------------------------ real files

def _real(name):
    hits = glob.glob(os.path.join(DAMOS, "**", name), recursive=True)
    if not hits:
        pytest.skip("%s not in Reference/EDC17/DAMOS" % name)
    return hits[0]


@pytest.mark.parametrize("name,size,epk", [
    ("03L906022LS_3610_398756_P680_I3W1_EDC17CP14_2.4.sgm", 0x200000, b"C680I3W1"),
    ("03L906022QF_6211_505923_P680_I3WF_EDC17CP14_2.4.sgm", 0x200000, b"C680I3WF"),
    ("03L906022KB_7088.sgm", 0x400000, b"C908K3WL"),
])
def test_real_sgm_decodes_every_block_to_the_image_of_its_damos_project(name, size, epk):
    p = _real(name)
    c = sgm.parse(open(p, "rb").read(), p)
    assert c.sections and not c.unrecognized
    assert all(s.scheme == "bcb1" and s.bcb1_key == bcb1.KEY_EDC17 for s in c.sections)
    assert not [b for b in c.blocks if b.problem]
    for s, b in zip(c.sections, c.blocks):
        assert len(s.plaintext) == b.declared_len
    base, img = sgm.image(c)
    assert len(img) == size
    # the EPK of the A2L project this container belongs to is in its image
    assert b"35/1/EDC17_CP14/5/" in img and epk in img
    assert c.sa2_tape == bytes.fromhex("6805814a05870a221289494c")


def test_real_sgm_decodes_where_its_damaged_sgo_cannot():
    sgm_p = _real("03L906022LS_3610_398756_P680_I3W1_EDC17CP14_2.4.sgm")
    # (the .sgo that came with it has '?' where 0x81/0x8D/0x8F/0x90/0x9D were,
    # so it is not kept in Reference; its absence is not a failure)
    c = sgm.parse(open(sgm_p, "rb").read())
    assert [hex(s.addr) for s in c.sections] == ["0x4000", "0x20000", "0x1a0000", "0x180000"]

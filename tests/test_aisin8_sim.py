# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Aisin 8-speed (VW "TR-80SD", 0C8 AL991/AQ250, 09G AQ450) UDS bench-sim
profile. No Qt: ecu_sim is the Qt-free ISO-TP/UDS engine.

The image-serving tests need a real plaintext AL991 FRF; they look for it under
``Reference/Aisin8/`` (overridable with ``AISIN8_REFERENCE``) and skip if it is
not there, like the ZF reference tests. The profile/security/geometry tests need
no file and always run.
"""
import os
import glob
import pytest

from ovtsuite.flash import ecu_sim as S
from ovtsuite.flash import crypto as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.environ.get("AISIN8_REFERENCE") or os.path.join(ROOT, "Reference", "Aisin8")


def _al991_frf():
    """A plaintext (early AL991, BYTESUM16) 0C8 FRF, or skip."""
    cands = sorted(glob.glob(os.path.join(REF, "FL_0C8927750*.frf")))
    if not cands:
        pytest.skip("no Aisin8 reference FRF under %s" % REF)
    return cands[0]


# -- profile / security / geometry (no file) --------------------------------
def test_profiles_registered():
    names = S.profile_names()
    assert "aisin8" in names and "aisin8-aq450" in names
    assert "TR-80SD" in S.profile("aisin8").title
    assert "AQ450" in S.profile("aisin8-aq450").title


def test_sa2_write_key_matches_tape():
    # The profile's sendKey (0x12) fn must reproduce sa2_execute over the baked
    # tape -- verified values for seed 0x11223344 (see trx80 project doc).
    for prof_name, tape, want in [
            ("aisin8", S.AISIN8_0C8_WRITE_TAPE, 0x785D0D8D),
            ("aisin8-aq450", S.AISIN8_09G_WRITE_TAPE, 0x83B5A89D)]:
        fn = S.profile(prof_name).key_fns[0x12]
        seed = b"\x11\x22\x33\x44"
        assert fn(seed) == want.to_bytes(4, "big")
        assert fn(seed) == C.sa2_execute(tape, 0x11223344).to_bytes(4, "big")


def test_0c8_and_09g_tapes_differ():
    # Same algorithm family, different leading FOR count -> different keys.
    assert S.AISIN8_0C8_WRITE_TAPE != S.AISIN8_09G_WRITE_TAPE
    s = 0x11223344
    assert C.sa2_execute(S.AISIN8_0C8_WRITE_TAPE, s) != C.sa2_execute(S.AISIN8_09G_WRITE_TAPE, s)


def test_verify_geometry_from_checksum_trailers():
    vb = S.profile("aisin8").verify_blocks
    assert vb[0x01] == (0x040000, 0x0FFFF0)   # block0 base + checksummed length
    assert vb[0x02] == (0x005000, 0x03AFF0)   # block1 base + checksummed length


def test_fallback_idents():
    idents = S.profile("aisin8").idents
    assert idents[0xF187] == b"0C8927750J"
    assert b"EV_TCMAL991031" in idents[0xF19E]


# -- image assembly + served UDS exchange (needs the FRF) -------------------
def test_assembles_plaintext_image():
    img = S.aisin8_image_from_frf(_al991_frf())
    if not img:
        pytest.skip("frf key/support unavailable in this environment")
    assert len(img) == 0x140000                       # covers block0 end 0x13FFFF
    assert img[:0x5000] == b"\xFF" * 0x5000            # gap before block1
    assert img[0x040000:0x040008] == bytes.fromhex("000003a0ffff7980")  # block0 head
    assert img[0x005000:0x005004] == bytes.fromhex("00013b01")          # block1 head


def test_served_handshake_and_identification():
    img = S.aisin8_image_from_frf(_al991_frf())
    if not img:
        pytest.skip("frf key/support unavailable in this environment")
    r = S.EcuResponder(S.aisin8_profile(), image=img, log=lambda s: None)

    assert r.handle(bytes.fromhex("1003"))[0] == 0x50          # session positive
    seed = r.handle(bytes.fromhex("2711"))                     # requestSeed 0x11
    assert seed[0] == 0x67 and seed[1] == 0x11
    key = C.sa2_execute(S.AISIN8_0C8_WRITE_TAPE,
                        int.from_bytes(seed[2:], "big")).to_bytes(4, "big")
    sk = r.handle(bytes([0x27, 0x12]) + key)                   # sendKey 0x12
    assert sk[0] == 0x67 and sk[1] == 0x12                     # key accepted

    assert b"0C8927750J" in r.handle(bytes.fromhex("22F187"))  # PN
    assert b"EV_TCMAL991031" in r.handle(bytes.fromhex("22F19E"))  # ODX id

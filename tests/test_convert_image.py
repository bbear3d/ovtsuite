# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""BIN / Intel HEX / S-record, and the TunerPro checksum plugin blocks."""

import os
import struct

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import checksums as K
from ovtsuite.convert import image as I

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")
HEX = os.path.join(ROOT, "Reference", "_incoming", "2026-10-14", "028906021CK_MSA15.hex")


def test_hex_and_srec_round_trip_segments():
    im = I.Image([(0x80000, bytes(range(256)) * 300), (0x100000, b"\x11" * 70000)])
    for fmt in ("hex", "srec"):
        assert I.read(I.write(im, fmt)).segments == im.segments


def test_bad_checksum_is_not_a_hex():
    good = I.write(I.Image([(0, b"\x01\x02\x03")]), "hex")
    bad = good.replace(b":03000000010203F7", b":03000000010203F8")
    assert I.detect(good) == "hex" and I.detect(bad) == "bin"


def test_merge_refuses_overlap():
    with pytest.raises(I.ImageError):
        I.merge([I.Image([(0, b"\0" * 16)]), I.Image([(8, b"\0" * 16)])])


def test_reference_hex_matches_place_reader():
    if not os.path.isfile(HEX):
        pytest.skip("reference file missing")
    from ovtsuite.definitions.place import _intel_hex
    data = open(HEX, "rb").read()
    assert I.read(data).flat() == _intel_hex(data)[0]


def test_binary_through_hex_and_back(tmp_path):
    if not os.path.isfile(LQ):
        pytest.skip("reference file missing")
    C.run(C.Job(sources=[LQ], target="hex", out=str(tmp_path / "a.hex")))
    C.run(C.Job(sources=[str(tmp_path / "a.hex")], target="bin",
                out=str(tmp_path / "a.bin")))
    assert (tmp_path / "a.bin").read_bytes() == open(LQ, "rb").read()


def test_edc16_descriptor_and_auto(tmp_path):
    if not os.path.isfile(LQ):
        pytest.skip("reference file missing")
    assert any(d.id == "edc16-flash" for d in K.descriptors())
    xdf = os.path.join(ROOT, "Reference", "XDFs", "038997016Q-BEW-BHW.xdf")
    if not os.path.isfile(xdf):
        pytest.skip("reference file missing")
    res = C.run(C.Job(sources=[xdf], target="xdf", binary=LQ, checksum="auto",
                      out=str(tmp_path / "o.xdf")))
    text = open(res.out[0]).read()
    assert "b3c7a1d2-4f8e-4a3b-9c6d-1e2f3a4b5c6d" in text and "0x1FFFFF" in text


def _pe_with_guid(path, guid: bytes):
    """A minimal PE: MZ, PE header, one data section holding ``guid``."""
    data = bytearray(0x400)
    data[0:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, 0x14C, 1)
    struct.pack_into("<H", data, 0x94, 0)
    sec = 0x80 + 24
    data[sec:sec + 8] = b".rdata\0\0"
    struct.pack_into("<II", data, sec + 16, 0x100, 0x200)
    struct.pack_into("<I", data, sec + 36, 0x40000040)
    data[0x210:0x210 + len(guid)] = guid
    path.write_bytes(bytes(data))


def test_plugin_dll_read_statically(tmp_path):
    p = tmp_path / "plugin.dll"
    _pe_with_guid(p, b"4C44220A-AD76-4B24-AAC2-AB384E29E622")
    info = K.read_plugin(str(p))
    assert info.guids == ["4c44220a-ad76-4b24-aac2-ab384e29e622"]
    assert info.machine == "x86"

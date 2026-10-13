# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""EDC16 identification-record extraction."""
from ovtsuite import fileinfo as F


def _image(hw, sw, ver, name, bosch="1037513681", dsid="P350U8A9"):
    d = bytearray(b"\xFF" * 0x200000)
    d[0x180010:0x180010 + 18] = (bosch + dsid).encode()
    rec = hw.ljust(12).encode() + b"\x00" * 8 + sw.ljust(12).encode() + b"\x00" + ver.encode() + b"\x00" + name.ljust(20).encode()
    d[0x1C0DE6:0x1C0DE6 + len(rec)] = rec
    return bytes(d)


def test_ident16_parses_record_and_block_header():
    # The definitive part number is the SECOND record field ("part"); the first
    # field, when itself a VAG number, is the hardware number.
    info = F.ident16(_image("038906016K", "038997016R", "0282", "R4 1,9L EDC"), (0x1BC000, 0x1E8000))
    assert info["part"] == "038997016R" and info["hw_part"] == "038906016K"
    assert info["sw_version"] == "0282" and info["system"] == "R4 1,9L EDC"
    assert info["bosch_sw"] == "1037513681" and info["dataset_id"] == "P350U8A9"
    assert info["project"] == "P350"
    # When both fields are the same part number, hw_part is not repeated.
    info = F.ident16(_image("03G906016HQ", "03G906016HQ", "9971", "R4 2,0L EDC"))
    assert info["part"] == "03G906016HQ" and info["sw_version"] == "9971"
    assert "hw_part" not in info


def test_ident16_missing_record():
    info = F.ident16(b"\xFF" * 0x200000)
    assert "part" not in info and "hw_part" not in info

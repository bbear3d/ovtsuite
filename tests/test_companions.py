# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Companions: an ECU's EEPROM (EDC15, EDC16) and the EDC16 internal flash
(MPC) are sets of their own -- versioned and exported on their own -- linked
from the flash set they go with (Project.link_companion). Synthetic images."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import platforms  # noqa: E402
from ovtsuite.platforms import identify as ident  # noqa: E402


def mpc_image(size=0x81000, hw=b"1037391503", sw=b"P475AAXE", fill=0xFF) -> bytes:
    """An EDC16U31-style internal flash: exception table at 0, ident string at
    0x4010, FADECAFE/CAFEAFFE descriptor at 0x403C."""
    img = bytearray([fill]) * size
    img[:0x100] = bytes(0x100)
    for i in range(0, 0x40, 8):
        img[i:i + 4] = (0x48001193 + i).to_bytes(4, "big")
    img[0x4010:0x4010 + 18] = hw + sw
    img[0x403C:0x4044] = b"\xFA\xDE\xCA\xFE\xCA\xFE\xAF\xFE"
    for i in range(0x5000, size - 0x1000, 4):         # some "code"
        img[i:i + 4] = ((i * 2654435761) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(img)


def test_an_mpc_image_is_recognised_by_structure():
    for blob in (mpc_image(), mpc_image(size=0x80100), mpc_image(size=0x80000)):
        got = ident.identify(blob)
        assert got.platform == "edc16mpc", len(blob)
        assert got.facts["software"] == "P475AAXE" and got.facts["part"] == "1037391503"
        assert platforms.is_companion(got.platform)
        assert platforms.COMPANION_OF["edc16mpc"] == ("edc16", "mpc")
    assert platforms.companion_roles("edc16") == ["eeprom", "mpc"]
    assert platforms.companion_roles("edc15") == ["eeprom"]


def test_neither_a_calibration_nor_other_files_are_taken_for_one():
    from ovtsuite.platforms import edc16 as p
    cal = bytearray(b"\xFF" * 0x80000)                  # 512 KB calibration: the
    cal[0x1000:0x1008] = b"\xFA\xDE\xCA\xFE" + p.DESCRIPTOR   # descriptor, no table
    assert ident.identify(bytes(cal)).platform != "edc16mpc"
    assert not p.is_mpc(mpc_image(size=0x82000))          # too big
    assert not p.is_mpc(mpc_image()[:0x7FFFF])             # too small
    bad = bytearray(mpc_image())
    bad[8:12] = b"\x00\x00\x00\x00"                       # table broken
    assert not p.is_mpc(bytes(bad))
    assert not p.is_mpc(os.urandom(0x81000))


# --------------------------------------------------------------- the model
from test_eeprom_pairing import eeprom15, eeprom16, flash15  # noqa: E402
from ovtsuite.project import Project  # noqa: E402
from ovtsuite.project import companions as co  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402


def flash16() -> bytes:
    from ovtsuite.platforms import edc16 as p
    img = bytearray(b"\xFF" * p.FULL_SIZE)
    img[p.CAL_START + 0x40:p.CAL_START + 0x48] = b"\xFA\xDE\xCA\xFE" + p.DESCRIPTOR
    img[p.CAL_START + 0x30:p.CAL_START + 0x34] = p.CAL_MARKER
    return bytes(img)


def test_an_mpc_links_to_an_edc16_flash_only():
    p = Project("x")
    fl = imp.import_bytes(p, flash16(), "tune.bin")
    assert fl.platform == "edc16"
    m = imp.import_bytes(p, mpc_image(), "03G906016KN.MPC")
    assert m.platform == "edc16mpc" and [x.role for x in m.members] == ["mpc"]
    assert co.link(p, fl.id, m.id) == "mpc"
    assert co.injected(p, fl.id) == {"mpc": mpc_image()}
    f15 = imp.import_bytes(p, flash15(), "f15.bin")
    with pytest.raises(ValueError):
        co.link(p, f15.id, m.id)               # not an EDC15 companion
    e15 = imp.import_bytes(p, eeprom15(), "e.bin")
    with pytest.raises(ValueError):
        co.link(p, fl.id, e15.id)              # nor an EDC16 one


def test_a_companion_has_versions_of_its_own_and_the_flash_follows_its_head():
    p = Project("x")
    fl = imp.import_bytes(p, flash16(), "tune.bin")
    m = imp.import_bytes(p, mpc_image(), "k.MPC")
    co.link(p, fl.id, m.id)
    flash_versions = len(fl.checkpoints)
    new = bytearray(mpc_image())
    new[0x6000:0x6004] = b"\x01\x02\x03\x04"
    p.commit(m.id, None, {"mpc": bytes(new)}, label="MPC edit")
    assert len(m.checkpoints) == 2 and len(fl.checkpoints) == flash_versions
    assert co.injected(p, fl.id)["mpc"] == bytes(new)           # follows the head
    # an edit made in the flash's tab goes to the companion
    assert co.commit_edits(p, fl.id, {"mpc": mpc_image()}) == [m.id]
    assert len(m.checkpoints) == 3 and len(fl.checkpoints) == flash_versions


def test_the_working_file_carries_the_companions_and_a_save_routes_them_back(tmp_path):
    from ovtsuite.project import sessionbridge as sb
    p = Project("x")
    fl = imp.import_bytes(p, flash16(), "tune.bin")
    e = imp.import_bytes(p, eeprom16(), "e.bin")
    m = imp.import_bytes(p, mpc_image(), "k.MPC")
    co.link(p, fl.id, e.id)
    co.link(p, fl.id, m.id)
    ext, data = sb.working_file(p, fl.id, p.state(fl.id, fl.active.head))
    path = tmp_path / ("w" + ext)
    path.write_bytes(data)
    images, _ = sb.read_back(p, fl.id, str(path))
    assert images["@eeprom"] == eeprom16() and images["@mpc"] == mpc_image()
    assert set(k for k in images if not k.startswith("@")) == {"flash"}


def test_a_dim_or_backup_style_flash_is_split_into_companion_sets():
    p = Project("x")
    s = p.add_set("old", platform="edc16")
    from ovtsuite.project.model import Member
    for mid in ("flash", "mpc", "eeprom"):
        p.add_member(s.id, Member(id=mid, role=mid, platform="edc16"))
    p.add_root(s.id, {"flash": flash16(), "mpc": mpc_image(), "eeprom": eeprom16()},
               kind="import", label="Imported old.dim")
    made = co.split(p, s.id)
    assert sorted(c.platform for c in made) == ["edc16eeprom", "edc16mpc"]
    assert co.injected(p, s.id) == {"mpc": mpc_image(), "eeprom": eeprom16()}
    assert co.split(p, s.id) == []                                # once
    assert len(p.sets) == 3

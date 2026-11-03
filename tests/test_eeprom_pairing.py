# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An ECU's EEPROM image is recognised as one (EDC15 512 bytes, EDC16 4 KB),
imports as a set of its own that opens in the EEPROM editor, and pairs with the
flash of the same control unit -- as its ``eeprom`` member -- in either
order. Needs the EDC15 / EDC16 modules (they know the layouts)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ovtsuite import platforms  # noqa: E402
from ovtsuite.platforms import identify as ident  # noqa: E402
from ovtsuite.project import Project  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402

edc15 = pytest.importorskip("ovtsuite.edc15.eeprom15")
edc16 = pytest.importorskip("ovtsuite.edc16.eeprom16")


def eeprom15(vin=b"WVWZZZ1JZ3W000001", odo=12345) -> bytes:
    """A checksummed EDC15 EEPROM image built with the module's own writer."""
    img = edc15.fix_checksums(bytes(512))
    f = edc15.FIELD_BY_KEY
    img = edc15.apply(img, f["vin"], vin.decode())
    return edc15.fix_checksums(img)


def eeprom16() -> bytes:
    """A 4 KB EDC16 EEPROM whose canonical blocks carry their own checksums."""
    img = bytearray(4096)
    for i in range(0x40, 0x1000):                  # content in every block
        img[i] = (i * 7 + 3) & 0xFF
    for base, counter in edc16.CANON_TABLE:
        edc16._set_chk(img, base, (counter - edc16._sum16(img, base)) & 0xFFFF)
    return bytes(img)


def flash15() -> bytes:
    from ovtsuite.platforms import edc15 as p
    img = bytearray(b"\xFF" * 0x80000)
    img[0x1000:0x1000 + len(p.DATASET_SIG)] = p.DATASET_SIG
    return bytes(img)


def test_an_edc15_eeprom_is_recognised_not_unrecognised():
    got = ident.identify(eeprom15())
    assert got.platform == "edc15eeprom" and got.known
    assert got.describe().startswith("Bosch EDC15 EEPROM")
    assert platforms.is_eeprom(got.platform)
    assert platforms.EEPROM_OF["edc15eeprom"] == "edc15"


def test_non_eeprom_binaries_stay_unrecognised():
    for blob in (bytes(512), b"\xFF" * 512, os.urandom(512), b"\xFF" * 4096,
                 bytes(1000)):
        assert ident.identify(blob).platform == "unknown"
    # a 512-byte file with one checksum byte wrong is not "its checksums agree"
    bad = bytearray(eeprom15())
    bad[0x4E] ^= 0xFF
    assert ident.identify(bytes(bad)).platform == "unknown"


def test_the_vin_names_the_project_information():
    got = ident.identify(eeprom15())
    assert got.facts.get("vin") == "WVWZZZ1JZ3W000001"
    assert ident.read_info(got) == {"vin": "WVWZZZ1JZ3W000001"}


def test_import_makes_a_set_of_its_own_holding_the_eeprom():
    p = Project("x")
    s = imp.import_bytes(p, eeprom15(), "my car.bin")
    assert s.platform == "edc15eeprom" and s.kind == "ecu"
    assert [(m.id, m.role) for m in s.members] == [("eeprom", "eeprom")]
    assert p.state(s.id, s.active.head)["eeprom"] == eeprom15()
    assert imp.standalone_eeprom_sets(p, "edc15") == [s]


def test_pairing_an_eeprom_with_a_flash_set():
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    assert fl.platform == "edc15"
    assert imp.flash_sets_for(p, "edc15") == [fl]
    out = imp.pair_eeprom(p, fl.id, eeprom15(), "car.bin")
    assert out is fl
    assert fl.member("eeprom").role == "eeprom"
    st = p.state(fl.id, fl.active.head)
    assert st["eeprom"] == eeprom15() and len(st["flash"]) == 0x80000
    # the older version of the branch has no EEPROM, the newer one has
    assert "eeprom" not in p.state(fl.id, fl.active.head and fl.checkpoints[0].id)
    # a second EEPROM replaces it as a new version; history keeps both
    other = eeprom15(vin=b"WVWZZZ1JZ3W000002")
    imp.pair_eeprom(p, fl.id, other, "car2.bin")
    assert p.state(fl.id, fl.active.head)["eeprom"] == other


def test_pairing_refuses_another_control_units_eeprom():
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    with pytest.raises(imp.ImportRefused):
        imp.pair_eeprom(p, fl.id, b"\x00" * 100, "junk.bin")


def test_a_standalone_eeprom_set_merges_into_a_flash_set():
    p = Project("x")
    e = imp.import_bytes(p, eeprom15(), "car.bin")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    out = imp.merge_eeprom_set(p, e.id, fl.id)
    assert out is fl and e not in p.sets
    assert p.state(fl.id, fl.active.head)["eeprom"] == eeprom15()
    assert imp.standalone_eeprom_sets(p) == []


def test_round_trip_through_the_project_file():
    p = Project("x")
    fl = imp.import_bytes(p, flash15(), "flash.bin")
    imp.pair_eeprom(p, fl.id, eeprom15(), "car.bin")
    imp.import_bytes(p, eeprom15(vin=b"WVWZZZ1JZ3W000009"), "other.bin")
    q = Project.from_bytes(p.to_bytes())
    assert sorted(s.platform for s in q.sets) == ["edc15", "edc15eeprom"]
    f2 = next(s for s in q.sets if s.platform == "edc15")
    assert q.state(f2.id, f2.active.head)["eeprom"] == eeprom15()


def test_an_edc16_eeprom_is_recognised_and_pairs_with_an_edc16_flash():
    img = eeprom16()
    got = ident.identify(img)
    assert got.platform == "edc16eeprom", got.describe()
    p = Project("x")
    e = imp.import_bytes(p, img, "ee.bin")
    assert e.platform == "edc16eeprom"
    # an EDC15 flash set is no home for it
    fl15 = imp.import_bytes(p, flash15(), "f15.bin")
    with pytest.raises(imp.ImportRefused):
        imp.pair_eeprom(p, fl15.id, img, "ee.bin")
    assert imp.standalone_eeprom_sets(p, "edc16") == [e]
    assert imp.standalone_eeprom_sets(p, "edc15") == []


def test_file_information_of_an_eeprom_and_of_a_paired_one():
    from ovtsuite import fileinfo
    rows = dict(fileinfo.eeprom_rows(eeprom15()))
    assert rows["EEPROM"] == "Bosch EDC15, 512 bytes"
    assert rows["  VIN"] == "WVWZZZ1JZ3W000001"
    assert "valid" in rows["  Checksums"]
    assert fileinfo.eeprom_rows(b"\x00" * 100) == []


def test_an_eeprom_read_with_bad_checksums_is_still_an_eeprom_set():
    bad = bytearray(eeprom15())
    bad[0x4E] ^= 0xFF
    p = Project("x")
    s = imp.import_read(p, bytes(bad), "EEPROM read", eeprom_of="edc15")
    assert s.platform == "edc15eeprom"
    assert [(m.id, m.role) for m in s.members] == [("eeprom", "eeprom")]
    assert p.state(s.id, s.active.head)["eeprom"] == bytes(bad)
    # without the hint the same bytes are no known binary
    assert imp.import_read(Project("y"), bytes(bad), "x").platform == "unknown"


def test_a_project_saved_before_eeprom_sets_adopts_them_on_open():
    """A set an older version stored as an unrecognised binary (the EEPROM
    imported as a raw file) becomes an EEPROM set when it is opened."""
    from ovtsuite.project import sessionbridge as sb
    p = Project("old")
    s = p.add_set("ee", platform="unknown")
    p.add_member(s.id, imp._member("flash", "raw", "unknown", name="ee.bin"))
    p.add_root(s.id, {"flash": eeprom15()}, kind="import", label="Imported ee.bin")
    st = p.state(s.id, s.active.head)
    assert sb.set_platform(p, s.id, st) == "edc15eeprom"
    assert s.members[0].role == "eeprom"

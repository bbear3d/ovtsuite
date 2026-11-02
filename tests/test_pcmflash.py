# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""ovtsuite.pcmflash: PCMFlash's one-file EDC16 reads (external flash +
internal flash [+ EEPROM]) taken apart, imported and put back."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import pcmflash as P  # noqa: E402
from ovtsuite.project import exporters as ex  # noqa: E402
from ovtsuite.project import importers as imp  # noqa: E402
from ovtsuite.project.project import Project  # noqa: E402

DESC = b"\xFA\xDE\xCA\xFE\xCA\xFE\xAF\xFE"


def _ext():
    b = bytearray(os.urandom(0x2000) * (P.EXT_SIZE // 0x2000))
    b[0x4003C:0x40044] = DESC
    return bytes(b)


def _mpc(n=0x80200):
    b = bytearray(os.urandom(0x2000) * (0x80000 // 0x2000))
    b[0x403C:0x4044] = DESC
    return bytes(b) + b"\xFF" * (n - 0x80000)


def _eeprom():
    return bytes(range(256)) * 16


def test_split_ext_then_micro():
    e, m = _ext(), _mpc()
    sp = P.split(e + m)
    assert sp.layout.order == ("ext", "mpc") and sp.layout.mpc_len == 0x80200
    assert sp.ext == e and sp.eeprom is None
    assert len(sp.mpc) == P.MPC_SIZE and sp.mpc[:0x80200] == m
    assert P.join(sp.layout, sp.ext, sp.mpc) == e + m


def test_split_with_eeprom_and_other_orders():
    e, m, ee = _ext(), _mpc(), _eeprom()
    for order, blob in ((("ext", "mpc", "eeprom"), e + m + ee),
                        (("eeprom", "ext", "mpc"), ee + e + m),
                        (("mpc", "ext", "eeprom"), m + e + ee)):
        sp = P.split(blob)
        assert sp.layout.order == order
        assert (sp.ext, sp.eeprom) == (e, ee)
        assert P.join(sp.layout, sp.ext, sp.mpc, sp.eeprom) == blob


def test_padded_internal_image_is_not_an_eeprom():
    # 0x80000 + 4 KB EEPROM and a 0x81000 internal image share a length
    e, m = _ext(), _mpc(0x81000)
    sp = P.split(e + m)
    assert sp.eeprom is None and sp.layout.mpc_len == 0x81000
    assert P.split(e + m[:0x80000] + _eeprom()).eeprom == _eeprom()


def test_other_files_are_left_alone():
    assert P.split(os.urandom(P.EXT_SIZE)) is None
    assert P.split(os.urandom(P.EXT_SIZE + 0x80200)) is None   # no descriptors
    assert P.split(b"\xFF" * 1000) is None


def test_project_import_and_exports():
    e, m, ee = _ext(), _mpc(), _eeprom()
    blob = e + m + ee
    proj = Project("t")
    s = imp.import_bytes(proj, blob, "full.bin")
    assert sorted(x.id for x in s.members) == ["eeprom", "flash", "mpc"]
    assert [o.format for o in s.originals] == ["pcmflash"]
    assert ex.export_raw(proj, s.id, "flash") == e
    # the BDM-style internal image: 0x81000 (516 KB)
    mpc = ex.export_raw(proj, s.id, "mpc")
    assert len(mpc) == 0x81000 and mpc[:0x80200] == m
    assert ex.export_raw(proj, s.id, "eeprom") == ee
    # unchanged: the original file, byte for byte
    oid = s.originals[0].id
    assert ex.export_original(proj, s.id, oid) == blob


@pytest.mark.skipif(not os.path.exists(r"C:\Users\bbear3D\Documents\EDC16\Alberta-BPW-BHW"),
                    reason="sample files not present")
def test_pcmflash_samples():
    d = r"C:\Users\bbear3D\Documents\EDC16\Alberta-BPW-BHW"
    names = os.listdir(d)
    pick = lambda k: open(os.path.join(d, next(n for n in names if k in n
                                               and n.startswith("EDC16_"))), "rb").read()
    full, ext, micro = pick("_flash_"), pick("_ext_"), pick("_micro_")
    sp = P.split(full)
    assert sp.ext == ext and sp.mpc[:len(micro)] == micro
    assert P.join(sp.layout, sp.ext, sp.mpc) == full


def test_export_pcmflash_from_any_set():
    e, m, ee = _ext(), _mpc(0x81000), _eeprom()
    proj = Project("t")
    s = imp.import_bytes(proj, e + m, "x.bin")           # members from a split
    # drop the PCMFlash original: this set now looks like one built from parts
    s.originals.clear()
    out = ex.export_pcmflash(proj, s.id)
    assert len(out) == P.EXT_SIZE + 0x80200
    sp = P.split(out)
    assert sp.layout.order == ("ext", "mpc") and sp.layout.mpc_len == 0x80200
    assert sp.ext == e and sp.mpc == m
    # with an EEPROM, when the set has one and it is asked for
    p2 = Project("t3")
    s3 = imp.import_bytes(p2, e + m + ee, "y.bin")
    out2 = ex.export_pcmflash(p2, s3.id, with_eeprom=True)
    assert P.split(out2).eeprom == ee and out2[:P.EXT_SIZE] == e
    assert len(ex.export_pcmflash(p2, s3.id)) == P.EXT_SIZE + 0x80200


def test_export_pcmflash_needs_both_flashes():
    proj = Project("t")
    s = imp.import_bytes(proj, os.urandom(10) + b"\x00" * 100, "z.bin")
    with pytest.raises(ex.ExportRefused):
        ex.export_pcmflash(proj, s.id)

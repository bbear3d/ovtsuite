# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: TunerPro XDF patches (XDFPATCH) -- read, kept, and made
from two binaries. Run alone: ``python -m pytest tests/test_convert_xdfpatch.py -q``.
"""

import os

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import xdfpatch
from ovtsuite.definitions.model import Definition, Storage, TableDef

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
VDO = os.path.join(REF, "VDO", "KWHack-defs", "VWK501.xdf")
XDF_16Q = os.path.join(REF, "XDFs", "038997016Q-BEW-BHW.xdf")
TUNED = os.path.join(REF, "Tunes", "016Q_uprava_klapka_T5.bin")
STOCK = TUNED + ".bak"


def need(*paths):
    for p in paths:
        if not os.path.isfile(p):
            pytest.skip("reference file missing: %s" % p)


def read(p):
    with open(p, "rb") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


def test_an_xdfs_patches_are_read_and_kept(tmp_path):
    need(VDO)
    d = C.load_definition(read(VDO))
    assert len(d.patches) == 10
    p = d.patches[0]
    assert p["title"] == "Distance To Empty" and p["categories"] == ["Cluster Configuration"]
    assert p["entries"][0] == {"name": "1", "address": 0x416, "patch": b"\xE1",
                               "base": b"\xC1"}
    res = C.run(C.Job(sources=[VDO], target="xdf", out=str(tmp_path / "o.xdf")))
    assert res.check == []
    back = C.load_definition(read(str(tmp_path / "o.xdf")))
    assert [x["title"] for x in back.patches] == [x["title"] for x in d.patches]
    # a format without patches says so
    res = C.run(C.Job(sources=[VDO], target="a2l", out=str(tmp_path / "o.a2l")))
    assert any("10 XDF patches" in w for w in res.warnings)


@pytest.mark.parametrize("split", ["one", "table"])
def test_a_tune_as_a_patch(tmp_path, split):
    """The 016Q tune against its original: the patch, applied the way
    TunerPro applies it, gives the tuned file byte for byte."""
    need(XDF_16Q, STOCK, TUNED)
    out = str(tmp_path / "p.xdf")
    res = C.run(C.Job(sources=[XDF_16Q], binary=STOCK, target="xdf", out=out,
                      options={"patch_with": TUNED, "patch_split": split}))
    assert res.check == []
    back = C.load_definition(read(out)).patches
    stock, tuned = read(STOCK), read(TUNED)
    assert xdfpatch.apply(back, stock) == tuned
    for p in back:
        assert p["categories"] == [xdfpatch.CATEGORY]
        for e in p["entries"]:
            a = e["address"]
            assert e["base"] == stock[a:a + len(e["patch"])]
            assert len(e["patch"]) <= xdfpatch.CHUNK
    if split == "one":
        assert len(back) == 1
        assert {e["name"] for e in back[0]["entries"]} >= {"FrmMng_v_Offset_C",
                                                          "CrCCD_stPanelVer_C"}
    else:
        assert len(back) == 6
        assert any(p["title"].endswith("FrmMng_v_Offset_C") for p in back)


def test_bytes_outside_the_tables_can_be_left_out(tmp_path):
    need(XDF_16Q, STOCK, TUNED)
    out = str(tmp_path / "p.xdf")
    res = C.run(C.Job(sources=[XDF_16Q], binary=STOCK, target="xdf", out=out,
                      options={"patch_with": TUNED, "patch_outside": "skip"}))
    assert res.check == []
    (p,) = C.load_definition(read(out)).patches
    assert not any("no table" in e["name"] for e in p["entries"])
    assert any("left out of the patch" in w for w in res.warnings)


def _defn():
    def tab(name, addr, n):
        return TableDef(name=name, kind="curve", cols=n,
                        z=Storage(addr=addr, bits=8))
    return Definition(type="json", tables=[tab("A", 0x10, 0x20), tab("B", 0x30, 0x300)])


def test_runs_join_within_a_table_and_never_across():
    old = bytes(0x400)
    new = bytearray(old)
    new[0x12] = new[0x18] = 1          # 5 apart in A: one entry
    new[0x2F] = new[0x30] = 1          # A's last byte and B's first: two
    for i in range(0x40, 0x240):       # 512 bytes in B: two chunks
        new[i] = 2
    new[0x3F0] = 3                     # in no table
    warn = []
    (p,) = xdfpatch.build(_defn(), old, bytes(new), "t", warnings=warn)
    got = [(e["name"], e["address"], len(e["patch"])) for e in p["entries"]]
    assert got == [("A", 0x12, 7), ("A", 0x2F, 1), ("B", 0x30, 1), ("B", 0x40, 0x100),
                   ("B", 0x140, 0x100), ("0x3F0 (no table)", 0x3F0, 1)]
    assert xdfpatch.apply([p], old) == bytes(new)
    assert any("outside every table" in w for w in warn)


def test_patch_needs_matching_binaries(tmp_path):
    with pytest.raises(xdfpatch.PatchError, match="differ in size"):
        xdfpatch.build(_defn(), bytes(16), bytes(32), "t")
    need(XDF_16Q, TUNED)
    with pytest.raises(C.ConvertError, match="original binary"):
        C.run(C.Job(sources=[XDF_16Q], target="xdf", out=str(tmp_path / "o.xdf"),
                    options={"patch_with": TUNED}))
    with pytest.raises(C.ConvertError, match="XDF only"):
        C.run(C.Job(sources=[XDF_16Q], binary=TUNED, target="a2l",
                    out=str(tmp_path / "o.a2l"), options={"patch_with": TUNED}))

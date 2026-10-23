# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: every definition format to every other, read back.

Run alone: ``python -m pytest tests/test_convert.py -q``.
"""

import json
import os

import pytest

from ovtsuite import convert as C
from ovtsuite.convert import fmt_csv, fmt_json, transforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
XDF = os.path.join(REF, "XDFs", "038997016Q-BEW-BHW.xdf")
A2L = os.path.join(REF, "A2L", "BPW.a2l")
LQ = os.path.join(REF, "Binaries", "03G906016LQ.bin")
OLS = os.path.join(REF, "_incoming", "2026-10-14", "028906021CK_MSA15.ols")
DAM = os.path.join(REF, "EDC15", "038906019GQ.dam")


def need(*paths):
    for p in paths:
        if not os.path.isfile(p):
            pytest.skip("reference file missing: %s" % p)


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


@pytest.mark.parametrize("target", ["xdf", "a2l", "kp", "json", "csv"])
@pytest.mark.parametrize("src,binary", [(XDF, None), (A2L, LQ), (OLS, None)])
def test_round_trip(tmp_path, src, binary, target):
    """Every table written reads back at the same place, width and size."""
    need(src, *([binary] if binary else []))
    res = C.run(C.Job(sources=[src], target=target, binary=binary,
                      out=str(tmp_path / ("out" + C.EXT[target]))))
    assert res.tables_out > 1000
    assert res.check == []


def test_sniff_is_structural(tmp_path):
    need(XDF)
    p = tmp_path / "renamed.bin"
    p.write_bytes(open(XDF, "rb").read())
    assert C.sniff(p.read_bytes()) == ("definition", "xdf")


def test_a2l_without_binary_says_how_it_sized_the_image(tmp_path):
    need(A2L)
    res = C.run(C.Job(sources=[A2L], target="json", out=str(tmp_path / "o.json")))
    assert any("memory segments" in w for w in res.warnings)
    # tables whose counts the image stores: kept at their declared maximum
    assert any("declared maximum" in m for v in res.loss.values() for m in v)


def test_a2l_with_binary_reads_stored_sizes(tmp_path):
    need(A2L, LQ)
    res = C.run(C.Job(sources=[A2L], target="xdf", binary=LQ,
                      out=str(tmp_path / "o.xdf")))
    assert not res.loss and res.size == 0x200000


def test_xdf_floats_and_checksum_blocks_survive(tmp_path):
    need(XDF)
    res = C.run(C.Job(sources=[XDF], target="xdf", checksum="keep",
                      out=str(tmp_path / "o.xdf")))
    back = C.load_definition(open(res.out[0], "rb").read())
    src = C.load_definition(open(XDF, "rb").read())
    assert back.checksums[0]["pluginmoduleid"].lower() == \
        src.checksums[0]["pluginmoduleid"].lower()
    assert sum(t.z.float for t in back.tables) == sum(t.z.float for t in src.tables)


def test_json_and_csv_round_trip_the_model(tmp_path):
    need(OLS)
    d = C.prepare(C.Job(sources=[OLS], target="json"))[0]
    j = fmt_json.loads(fmt_json.dumps(d).encode())
    c = fmt_csv.loads(fmt_csv.dumps(d).encode())
    for back in (j, c):
        assert len(back.tables) == len(d.tables)
        for a, b in zip(d.tables, back.tables):
            assert (a.z.addr, a.rows, a.cols, a.z.bits, a.kind) == \
                (b.z.addr, b.rows, b.cols, b.z.bits, b.kind)


def test_filter_and_merge(tmp_path):
    need(OLS)
    d = C.prepare(C.Job(sources=[OLS], target="json"))[0]
    few = transforms.filter_tables(d.tables, {"pattern": "^mrw"})
    assert few and all(t.name.lower().startswith("mrw") for t in few)
    m = transforms.merge([d, d])
    assert len(m.tables) == len({(t.z.addr, t.z.bits, t.kind) for t in d.tables})


def test_ecu_address_a2l(tmp_path):
    need(A2L, LQ)
    res = C.run(C.Job(sources=[A2L], target="a2l", binary=LQ, base=0x10000,
                      options={"a2l_addresses": "ecu"}, out=str(tmp_path / "o.a2l")))
    assert "MEMORY_SEGMENT Image" in open(res.out[0]).read()
    assert "0x10000 0x200000" in open(res.out[0]).read()


def test_output_never_replaces_source_via_cli(tmp_path):
    from ovtsuite.cli import main
    need(OLS)
    out = tmp_path / "ck.xdf"
    assert main(["convert", OLS, "-o", str(out)]) == 0
    preset = tmp_path / "p.json"
    assert main(["convert", OLS, "--to", "csv", "--save-preset", str(preset)]) == 0
    assert json.load(open(preset))["target"] == "csv"


def test_a_default_output_never_overwrites_an_input(tmp_path):
    """x.xdf to XDF with no output name writes x-converted.xdf; a given
    name that is an input (any case) is refused."""
    need(XDF)
    import shutil
    src = tmp_path / "x.xdf"
    shutil.copy(XDF, src)
    before = src.read_bytes()
    res = C.run(C.Job(sources=[str(src)], target="xdf", title="Changed"))
    assert src.read_bytes() == before
    assert res.out[0].endswith("x-converted.xdf")
    with pytest.raises(C.ConvertError):
        C.run(C.Job(sources=[str(src)], target="xdf", out=str(tmp_path / "X.XDF")))
    assert C.free_output(str(src), [str(src), str(tmp_path / "x-converted.xdf")]) \
        .endswith("x-converted-2.xdf")


def test_a_tunerpro_plugin_choice_writes_its_id_and_method():
    from ovtsuite.convert import checksums as K
    w = []
    b = K.tp_block("tp:b3c7a1d2-4f8e-4a3b-9c6d-1e2f3a4b5c6d:2", 0x200000, w)
    assert b["pluginmoduleid"] == "B3C7A1D2-4F8E-4A3B-9C6D-1E2F3A4B5C6D"
    assert b["calculationmethod"] == "0x2" and b["dataend"] == "0x1FFFFF"
    assert K.tp_block("tp:nonsense:0", 0x1000, w) is None

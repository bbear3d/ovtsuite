# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Converter: a definition ported onto another software version, and
two definitions compared.

The truth for the port is the other software's own definition: 03G906016FK's
A2L (EDC16U1 U85B6) onto 03G906016AA's binary (U85B2), checked against AA's
A2L. Run alone: ``python -m pytest tests/test_convert_port.py -q``.
"""

import csv
import os

import pytest

from ovtsuite import convert as C

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")
FK = os.path.join(REF, "A2L", "03G906016FK_9971_399899_P379_U85B6_EDC16U1_5.41")
AA = os.path.join(REF, "A2L", "03G906016AA_5928_370480_P379_U85B2_EDC16U1_5.41")
AA_BIN = os.path.join(REF, "Binaries", "03G906016AA_U85B2_from_A2L_HEX.bin")


def need(*paths):
    for p in paths:
        if not os.path.isfile(p):
            pytest.skip("reference file missing: %s" % p)


@pytest.fixture(autouse=True)
def _own_config(tmp_path, monkeypatch):
    monkeypatch.setenv("OVT_CONVERTER_DIR", str(tmp_path / "cfg"))


def _truth():
    """AA's own tables, in AA's binary: name -> image offset."""
    d = C.prepare(C.Job(sources=[AA + ".a2l"], binary=AA_BIN, target="json"))[0]
    return {t.symbol or t.name: t.z.addr for t in d.tables}


def _ported(tmp_path, **opts):
    out = str(tmp_path / "ported.ovtdef.json")
    res = C.run(C.Job(sources=[FK + ".a2l"], binary=AA_BIN, target="json", out=out,
                      options=dict({"port": True}, **opts)))
    with open(out, "rb") as fh:
        return res, C.load_definition(fh.read())


def _score(defn, truth):
    right = wrong = unv = 0
    for t in defn.tables:
        n = t.symbol or t.name
        if n not in truth:
            continue
        if C.UNVERIFIED in t.categories:
            unv += 1
        elif truth[n] == t.z.addr:
            right += 1
        else:
            wrong += 1
    return right, wrong, unv


def test_port_with_the_original_image(tmp_path):
    """With FK's HEX the moved tables are found by their bytes: over 8,500
    confirmed, at most 0.3 % of them wrong."""
    need(FK + ".a2l", FK + ".HEX", AA + ".a2l", AA_BIN)
    res, defn = _ported(tmp_path, reference=FK + ".HEX")
    assert res.check == []
    assert res.placement["moved"] > 3000
    right, wrong, unv = _score(defn, _truth())
    assert right > 8500 and wrong <= 0.003 * right, (right, wrong)
    assert unv > 0
    # every unverified table says so in the report
    assert any("unverified" in m for v in res.loss.values() for m in v)


def test_port_by_structure_alone(tmp_path):
    """Without the original image fewer are confirmed; what is confirmed is
    right, the rest is set apart, not guessed."""
    need(FK + ".a2l", AA + ".a2l", AA_BIN)
    res, defn = _ported(tmp_path)
    right, wrong, unv = _score(defn, _truth())
    assert right > 6000 and wrong <= 0.005 * right, (right, wrong)
    assert unv > 1000


def test_port_can_leave_unconfirmed_tables_out(tmp_path):
    need(FK + ".a2l", AA_BIN)
    res, defn = _ported(tmp_path, unverified="skip")
    assert not any(C.UNVERIFIED in t.categories for t in defn.tables)
    assert any("left out" in m for v in res.loss.values() for m in v)


def test_port_needs_the_binary(tmp_path):
    need(FK + ".a2l")
    with pytest.raises(C.ConvertError, match="binary of the other software"):
        C.run(C.Job(sources=[FK + ".a2l"], target="xdf", out=str(tmp_path / "o.xdf"),
                    options={"port": True}))


def test_a_tune_carried_to_another_version(tmp_path):
    """FK's values (exported through FK's A2L from FK's HEX) applied onto AA's
    binary through FK's A2L ported there: every table written lands where
    AA's own A2L says it is, and no unconfirmed table is touched."""
    need(FK + ".a2l", FK + ".HEX", AA + ".a2l", AA_BIN)
    dcm = str(tmp_path / "fk.dcm")
    C.run(C.Job(sources=[FK + ".a2l"], binary=FK + ".HEX", target="dcm", out=dcm))
    out = str(tmp_path / "aa_with_fk.bin")
    res = C.run(C.Job(sources=[FK + ".a2l", dcm], binary=AA_BIN, target="bin", out=out,
                      options={"port": True, "reference": FK + ".HEX"}))
    assert res.check == []
    aside = [k for k, v in res.loss.items() if any("unverified" in m for m in v)]
    assert aside and all("not written" in " ".join(res.loss[k]) for k in aside)
    # read back through AA's own A2L: the written tables hold FK's values
    from ovtsuite.convert import calvalues as V
    with open(dcm, "rb") as fh:
        fk = {p.name: p for p in V.load(fh.read()).params}
    got_dcm = str(tmp_path / "aa.dcm")
    C.run(C.Job(sources=[AA + ".a2l"], binary=out, target="dcm", out=got_dcm))
    with open(got_dcm, "rb") as fh:
        aa = {p.name: p for p in V.load(fh.read()).params}
    same = differ = 0
    for n, p in fk.items():
        q = aa.get(n)
        if q is None or n in aside or (p.nx, p.ny) != (q.nx, q.ny) or p.kind == "axis":
            continue
        if V._close(p.z, q.z, 1e-9):
            same += 1
        else:
            differ += 1
    assert same > 7000 and differ <= 0.005 * same, (same, differ)


# ------------------------------------------------------------------ diff
def _write_pair(tmp_path):
    from ovtsuite.convert import fmt_json
    from ovtsuite.definitions.model import Definition, Storage, TableDef

    def tab(name, addr, rows=1, cols=1, bits=16, factor=1.0, units="", kind="constant"):
        return TableDef(name=name, kind=kind, rows=rows, cols=cols,
                        z=Storage(addr=addr, bits=bits, factor=factor, units=units,
                                  endian=">"))
    a = Definition(type="json", title="a", tables=[
        tab("SAME", 0x100), tab("MOVES", 0x200), tab("GONE", 0x300),
        tab("OLDNAME", 0x400), tab("GROWS", 0x500, 1, 4, kind="curve"),
        tab("SCALE", 0x600, factor=0.1, units="Nm"), tab("WIDTH", 0x700)])
    b = Definition(type="json", title="b", tables=[
        tab("SAME", 0x100), tab("MOVES", 0x210), tab("NEW", 0x800),
        tab("NEWNAME", 0x400), tab("GROWS", 0x500, 1, 6, kind="curve"),
        tab("SCALE", 0x600, factor=0.2, units="Nm"), tab("WIDTH", 0x700, bits=32)])
    pa, pb = tmp_path / "a.ovtdef.json", tmp_path / "b.ovtdef.json"
    pa.write_text(fmt_json.dumps(a, base=0, image_size=0x1000))
    pb.write_text(fmt_json.dumps(b, base=0, image_size=0x1000))
    return str(pa), str(pb)


def test_diff_names_every_kind_of_change(tmp_path):
    pa, pb = _write_pair(tmp_path)
    out = tmp_path / "d.diff.csv"
    res = C.run(C.Job(sources=[pa, pb], target="diff", out=str(out)))
    assert res.check == [] and not res.lossy
    rows = list(csv.reader(out.open()))
    assert rows[0] == ["name", "change", "a.ovtdef.json", "b.ovtdef.json"]
    got = {(r[0], r[1]) for r in rows[1:]}
    assert got == {("NEW", "added"), ("GONE", "removed"), ("OLDNAME", "renamed"),
                   ("MOVES", "moved"), ("GROWS", "resized"), ("SCALE", "rescaled"),
                   ("WIDTH", "retyped")}
    assert "1 added" in res.values[0] and "1 renamed" in res.values[0]


def test_diff_keeps_every_table_whose_name_repeats():
    """XDFs often reuse a title: each table of that name is compared, paired
    first with the one at the same place."""
    from ovtsuite.convert import transforms
    from ovtsuite.definitions.model import Definition, Storage, TableDef

    def tab(addr, factor=1.0):
        return TableDef(name="Torque limit", kind="constant", rows=1, cols=1,
                        z=Storage(addr=addr, bits=16, factor=factor, endian=">"))
    a = Definition(type="xdf", title="a", tables=[tab(0x100), tab(0x200), tab(0x300)])
    b = Definition(type="xdf", title="b", tables=[tab(0x300), tab(0x100, 0.5)])
    got = {(r["name"], r["change"]) for r in transforms.diff(a, b)}
    assert got == {("Torque limit [1]", "rescaled"), ("Torque limit [2]", "removed")}


def test_diff_takes_exactly_two_definitions(tmp_path):
    pa, _pb = _write_pair(tmp_path)
    with pytest.raises(C.ConvertError, match="two definitions"):
        C.run(C.Job(sources=[pa], target="diff", out=str(tmp_path / "d.csv")))


def test_diff_of_two_software_versions(tmp_path):
    need(FK + ".a2l", AA + ".a2l")
    res = C.run(C.Job(sources=[FK + ".a2l", AA + ".a2l"], target="diff",
                      out=str(tmp_path / "fk_aa.diff.csv")))
    kinds = {}
    for r in res.diff:
        kinds[r["change"]] = kinds.get(r["change"], 0) + 1
    assert kinds["moved"] > 8000 and kinds["added"] > 100 and kinds["removed"] > 40
    assert {"name": "CrCtl_facEnmRsu_C", "change": "retyped",
            "old": "32-bit", "new": "signed 16-bit"} in res.diff

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""OVT Diag definition packs that live inside platform modules
(``ovtsuite/<module>/definitions/<pack>/``): discovery through the module
registry, the generator that builds the EDC16 faults pack from the DSM
catalogue, and the byte-stable compiled form the module packages ship."""
import gzip
import os
import sys

import pytest

from ovtsuite.diag import defs as D
from ovtsuite.diag.defs import load as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def build_diag_defs():
    if TOOLS not in sys.path:
        sys.path.insert(0, TOOLS)
    mod = pytest.importorskip("build_diag_defs")
    if not os.path.isfile(mod.DSM_CATALOGUE):
        pytest.skip("no EDC16 DSM catalogue in this tree")
    return mod


# ---------------------------------------------------------------- discovery
def test_module_dirs_follow_the_registry(tmp_path, monkeypatch):
    """A module's definitions/ is found through the registry and the package's
    search path (source tree or a mounted .ovtmod alike); a module without
    one, and an id the registry does not list, contribute nothing."""
    pkg_a = tmp_path / "moda"
    pkg_b = tmp_path / "modb"
    _write(str(pkg_a / "definitions" / "moda-faults" / "pack.yaml"),
           "format: 1\nid: moda-faults\nversion: 0.1.0\ntitle: t\npublisher: p\nlicense: MPL-2.0\n")
    _write(str(pkg_a / "definitions" / "moda-faults" / "faults.yaml"),
           "format: 1\nkind: faults\nmatch: {addr: '01', platform: moda}\n"
           "codes:\n- {id: moda.fault.v00001, vag: 1, text: one}\n")
    os.makedirs(str(pkg_b))
    roots = {"moda": [str(pkg_a)], "modb": [str(pkg_b)], "ghost": [str(tmp_path / "nowhere")]}
    monkeypatch.setattr(L, "installed_module_ids", lambda errors=None: ["modb", "moda"])
    monkeypatch.setattr(L, "module_package_dirs", lambda mid: roots.get(mid, []))
    dirs = L.module_dirs()
    assert dirs == [str(pkg_a / "definitions")]
    packs = D.load_packs(dirs, "module")
    assert [p.id for p in packs] == ["moda-faults"] and packs[0].origin == "module"
    res = D.Resolver(packs)
    ident = D.Identity(addr="01", protocol="kwp2000", platform="moda")
    assert res.fault_text(ident, vag=1) == "one"
    # the platform match keeps it off another platform's module 01
    assert res.fault_text(D.Identity(addr="01", protocol="kwp2000", platform="other"), vag=1) == ""


def test_module_dirs_survive_a_missing_registry(monkeypatch):
    monkeypatch.setattr(L, "installed_module_ids", lambda errors=None: [])
    assert L.module_dirs() == []


def test_missing_registry_is_reported_not_swallowed(monkeypatch):
    """A registry that cannot be imported is said in ``errors`` (the Module
    Manager shows it) instead of silently leaving every module pack out."""
    import builtins
    real_import = builtins.__import__

    def no_registry(name, *a, **kw):
        if name.endswith("modules") and a and a[2] and "registry" in a[2]:
            raise ImportError("no registry here")
        return real_import(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_registry)
    errors = []
    assert L.installed_module_ids(errors) == []
    assert errors and errors[0][0] == "modules" and "no registry here" in errors[0][1]
    assert L.installed_module_ids() == []                      # without a list: quiet, as before


# ---------------------------------------------------------------- generator
def test_edc16_code_paths_keep_only_unambiguous_codes(build_diag_defs, tmp_path):
    """Every #nnnnn / Pxxxx token in a signature maps to that signature's
    path; a code seen with two paths is dropped, not guessed."""
    cat = tmp_path / "cat.json.gz"
    with gzip.open(str(cat), "wt", encoding="utf-8") as fh:
        fh.write('{"own": {}, "sig": {'
                 '"- - - - #12114 #12115 P1604": "AOHtCDHt3", '
                 '"- - #12114 n202 P0299": "PCR_GovDev", '
                 '"#3498 U0101 n1 -": "PFltChar"}}')
    single, dropped = build_diag_defs.edc16_code_paths(str(cat))
    assert dropped == {("vag", 12114)}
    assert single == {("vag", 12115): "AOHtCDHt3", ("sae", "P1604"): "AOHtCDHt3",
                      ("sae", "P0299"): "PCR_GovDev", ("vag", 3498): "PFltChar",
                      ("sae", "U0101"): "PFltChar"}


def test_edc16_fault_text_forms(build_diag_defs):
    t = build_diag_defs.fault_text
    # an SAE code with a standard description: that, then the path
    assert t("sae", "P0299", "PCR_GovDev").endswith("(fault path PCR_GovDev)")
    assert t("sae", "P0299", "PCR_GovDev") != "Fault path PCR_GovDev"
    # a VAG code: the path's plain-English name, then the path
    assert t("vag", 12114, "AOHtCDHt3") == "Aux Heater heater (fault path AOHtCDHt3)"
    # a path signal_names cannot name better than itself: the path alone
    assert t("sae", "C0014", "IFCD") == "Fault path IFCD"


def test_edc16_faults_pack_builds_validates_and_resolves(build_diag_defs, tmp_path):
    out = str(tmp_path / "edc16" / "definitions" / "edc16-faults")
    n, dropped = build_diag_defs.build_edc16_faults(out)
    assert n > 500 and dropped > 0
    pack = D.load_pack(out, "module")
    rep = D.validate_pack(pack)
    assert rep.ok, rep.text()
    assert not rep.warnings, rep.text()
    doc = pack.docs[0].body
    assert doc["source"] == "binary" and doc["confidence"] == "likely"
    assert doc["match"] == {"addr": "01", "platform": "edc16"}
    ids = [c["id"] for c in doc["codes"]]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    res = D.Resolver([pack])
    edc16 = D.Identity(addr="01", protocol="kwp2000", platform="edc16")
    assert res.fault_text(edc16, vag=12114) == "Aux Heater heater (fault path AOHtCDHt3)"
    assert "PCR_GovDev" in res.fault_text(edc16, sae="P0299")
    # an SAE entry whose P-code has a VAG number carries both keys, so the
    # same entry answers whichever word the ECU reports; C-codes have none
    p0299 = res.fault(edc16, sae="P0299").entry
    assert p0299["vag"] == 16683 and res.fault(edc16, vag=16683).entry is p0299
    both = [c for c in doc["codes"] if "vag" in c and "sae" in c]
    assert len(both) > 300 and all(c["sae"][0] == "P" for c in both)
    assert all("vag" not in c for c in doc["codes"] if c.get("sae", "P")[0] in "CBU")
    # not for EDC15's engine module
    assert res.fault(D.Identity(addr="01", protocol="kwp2000", platform="edc15"), vag=12114) is None
    # a second run writes nothing new and --check style comparison holds
    assert build_diag_defs.build_edc16_faults(out) == (n, dropped)
    # the per-part module documents: fault-word encoding by software family
    mods = pack.docs_of("module")
    assert len(mods) > 50 and all(d.match.addr == "01" and d.match.platform == "edc16" for d in mods)
    assert res.module(edc16.with_(part="03G906016LQ"))["faults"]["encoding"] == "sae"      # P475
    assert res.module(edc16.with_(part="03G906021MP"))["faults"]["encoding"] == "sae"      # P447, by prefix
    assert res.module(edc16.with_(part="038997016T"))["faults"]["encoding"] == "vag"       # P350, by prefix
    assert res.module(edc16.with_(part="03G906016AQ"))["faults"]["encoding"] == "vag"      # EDC16U1 / P379
    assert "faults" not in res.module(edc16.with_(part="4F0907401A"))                      # not in the catalogue
    assert "faults" not in res.module(edc16)                                               # no part known
    # a module document no longer generated is removed on the next run
    stale = os.path.join(out, build_diag_defs.MODULE_DOCS_DIR, "ZZZ999999.yaml")
    _write(stale, "kind: module\n")
    build_diag_defs.build_edc16_faults(out)
    assert not os.path.exists(stale)


def test_generated_yaml_is_lf_and_check_compares_bytes(build_diag_defs, tmp_path):
    out = str(tmp_path / "edc16" / "definitions" / "edc16-faults")
    build_diag_defs.build_edc16_faults(out)
    for fn in build_diag_defs._generated_files(out):
        with open(os.path.join(out, fn), "rb") as fh:
            assert b"\r" not in fh.read(), fn
    assert "faults.yaml" in build_diag_defs._generated_files(out)
    assert any(fn.startswith(build_diag_defs.MODULE_DOCS_DIR) for fn in build_diag_defs._generated_files(out))
    assert not build_diag_defs._write_if_changed(os.path.join(out, "pack.yaml"), build_diag_defs.edc16_pack_manifest())


def test_vag_number_of(build_diag_defs):
    f = build_diag_defs.vag_number_of
    assert f("P0299") == 16683 and f("P1604") == 18012 and f("P3000") == 19456       # 0x4000 + d*1024 + xyz
    assert f("C0014") is None and f("U0101") is None and f("P00F7") is None and f("P2A00") is None
    assert f("") is None


def test_compiled_pack_is_byte_stable(tmp_path):
    root = tmp_path / "p"
    _write(str(root / "pack.yaml"), "format: 1\nid: p\nversion: 1.0.0\ntitle: t\npublisher: p\nlicense: MPL-2.0\n")
    _write(str(root / "faults.yaml"), "format: 1\nkind: faults\nmatch: {addr: '01'}\n"
           "codes:\n- {id: p.fault.v00001, vag: 1, text: one}\n")
    a = D.compile_pack(str(root), str(tmp_path / "a.json.gz"))
    b = D.compile_pack(str(root), str(tmp_path / "b.json.gz"))
    with open(a, "rb") as fa, open(b, "rb") as fb:
        assert fa.read() == fb.read()
    assert D.load_pack(a).docs[0].body["codes"][0]["text"] == "one"

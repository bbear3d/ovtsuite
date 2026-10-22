# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Definition snapshot (ovtsuite.defn): a container carries the resolved
parameter table so reopening skips identification and matching -- and may
never change the answer, only skip the work."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from conftest import BINARIES, ZF6HP_REFERENCE  # noqa: E402
from ovtsuite import container as C  # noqa: E402
from ovtsuite import defn as D  # noqa: E402
from ovtsuite import history as H  # noqa: E402
from ovtsuite.session import TunerSession  # noqa: E402

EDC16 = os.path.join(BINARIES, "03G906016AB.bin")
EDC15 = os.path.join(BINARIES, "EDC15", "038906012BD.bin")
ZF = os.path.join(ZF6HP_REFERENCE, "3.0TDI-Stock.bin")


def _need(p):
    if not os.path.exists(p):
        pytest.skip("%s not present" % os.path.basename(p))
    return p


def _same_model(a, b):
    """Every entity, pairing key and platform attribute identical."""
    for k in list(getattr(a, "_lazy", None) or ()):
        getattr(a, k)
    for k in list(getattr(b, "_lazy", None) or ()):
        getattr(b, k)
    ea = list(a.entities) + (list(a.variant_entities())
                             if a.platform == "edc16" else [])
    eb = list(b.entities) + (list(b.variant_entities())
                             if b.platform == "edc16" else [])
    ca, cb = D._Enc(ea, a.kb), D._Enc(eb, b.kb)
    assert [D._erow(e) for e in ea] == [D._erow(e) for e in eb]
    assert (sorted(json.dumps([ca.enc(k), ca.enc(e)["$e"]])
                   for k, e in a.by_key.items())
            == sorted(json.dumps([cb.enc(k), cb.enc(e)["$e"]])
                      for k, e in b.by_key.items()))
    skip = D._SKIP
    for k, v in vars(a).items():
        if k.startswith("_") or k in skip:
            continue
        assert json.dumps(ca.enc(v), sort_keys=True) == json.dumps(
            cb.enc(vars(b)[k]), sort_keys=True), k


def _saved(src, ext, tmp_path):
    path = str(tmp_path / ("t" + ext))
    s = TunerSession(src)
    s.save(path, backup=False)
    return path, s


@pytest.mark.parametrize("src,ext", [(EDC16, ".edc16"), (EDC15, ".edc15"),
                                     (ZF, ".6hp")])
def test_a_reopened_container_uses_its_definition_and_changes_nothing(
        src, ext, tmp_path):
    path, built = _saved(_need(src), ext, tmp_path)
    assert built.defn_state == "none"
    again = TunerSession(path)
    assert again.defn_state == "used"
    assert again.platform == built.platform and again.family == built.family
    _same_model(built, again)
    # entities are wired to the live image, not to a copy of it
    e = next(e for e in again.entities if e.kind in ("map", "curve"))
    assert e.binfile is again.binfile


def test_tuning_values_keeps_the_definition_valid(tmp_path):
    path, _ = _saved(_need(EDC16), ".edc16", tmp_path)
    s = TunerSession(path)
    e = next(e for e in s.entities if e.name == "LIGov_nSetpointWarm_CUR")
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 10)])
    s.save(backup=False)
    again = TunerSession(path)
    assert again.defn_state == "used"
    e2 = next(x for x in again.entities if x.name == e.name)
    assert e2.cell(0, 0) == pytest.approx(e.cell(0, 0))


def test_a_changed_structure_falls_back_to_the_normal_path(tmp_path):
    path, built = _saved(_need(EDC16), ".edc16", tmp_path)
    c = C.Container.load(path)
    img = bytearray(c.external())
    covered = sorted((e.addr, e.end) for e in built.entities)
    # a byte no parameter cell owns: flip it as a code patch would
    probe = next(a for a in range(0x20000, 0x60000, 0x101)
                 if all(not (lo <= a < hi) for lo, hi in covered))
    img[probe] ^= 0xFF
    c.add(C.BLOCK_EXTERNAL, bytes(img))
    c.save(path)
    again = TunerSession(path)
    assert again.defn_state == "image structure changed"
    assert len(again.entities) == len(built.entities)   # built the slow way


def test_a_structural_edit_in_the_session_is_not_snapshotted(tmp_path):
    """A raw edit outside every cell and breakpoint (a count word, a header)
    leaves the in-memory table describing the OLD bytes. Saving must not
    snapshot it -- it would pass the mask check against the new bytes and
    reopen with stale shapes. (2026-10-10: ASDdc_dnIntegLtdPos_MAP restored
    4x4 -> 4x6 by raw bytes came back 4x4.)"""
    path, built = _saved(_need(EDC16), ".edc16", tmp_path)
    s = TunerSession(path)
    assert s.defn_state == "used"
    covered = sorted((e.addr, e.end) for e in s.entities)
    probe = next(a for a in range(0x20000, 0x60000, 0x101)
                 if all(not (lo <= a < hi) for lo, hi in covered))
    s.edit_bytes(probe, bytes([s.binfile.data[probe] ^ 0xFF]))
    assert D.build(s, bytes(s.binfile.data)) is None
    s.save(backup=False)
    again = TunerSession(path)
    assert again.defn_state != "used"                    # parsed from the bytes
    again.save(backup=False)                             # ... and re-snapshotted
    assert TunerSession(path).defn_state == "used"


@pytest.mark.parametrize("what", ["code", "data", "format"])
def test_a_stale_definition_is_ignored(what, tmp_path, monkeypatch):
    path, built = _saved(_need(EDC16), ".edc16", tmp_path)
    if what == "code":
        monkeypatch.setattr(D, "code_digest", lambda *a: "different")
    elif what == "data":
        monkeypatch.setattr(D, "data_digest", lambda *a: "different")
    else:
        monkeypatch.setattr(D, "FORMAT", D.FORMAT + 1)
    again = TunerSession(path)
    assert again.defn_state != "used" and again.defn_state != "none"
    assert len(again.entities) == len(built.entities)


def test_a_damaged_or_hostile_definition_cannot_break_or_run_anything(tmp_path):
    path, built = _saved(_need(ZF), ".6hp", tmp_path)
    c = C.Container.load(path)
    snap = json.loads(c.get(C.BLOCK_DEFN).data)
    # a node that names a class outside the package must be refused
    snap["attrs"]["model"] = {"$o": 999, "c": "os:system", "f": {}}
    c.add(C.BLOCK_DEFN, json.dumps(snap).encode())
    c.save(path)
    again = TunerSession(path)
    assert again.defn_state.startswith("unreadable")
    assert len(again.entities) == len(built.entities)
    # garbage is no worse
    c.add(C.BLOCK_DEFN, b"{not json")
    c.save(path)
    assert TunerSession(path).defn_state.startswith("unreadable")


def test_an_explicit_family_ignores_the_definition(tmp_path):
    path, built = _saved(_need(EDC16), ".edc16", tmp_path)
    assert TunerSession(path, family=built.family).defn_state == "none"


def test_a_container_without_a_definition_still_opens(tmp_path):
    path, built = _saved(_need(EDC16), ".edc16", tmp_path)
    c = C.Container.load(path)
    c.remove(C.BLOCK_DEFN)
    c.save(path)
    again = TunerSession(path)
    assert again.defn_state == "no snapshot"
    assert len(again.entities) == len(built.entities)


def test_the_definition_is_much_faster_than_building_it(tmp_path):
    import time
    path, _ = _saved(_need(ZF), ".6hp", tmp_path)
    t = time.time()
    TunerSession(ZF)
    slow = time.time() - t
    t = time.time()
    TunerSession(path)
    fast = time.time() - t
    assert fast < slow / 3, "stored definition %.2fs vs built %.2fs" % (fast, slow)


# -- another image of the same file shares the loaded definition -------------

def _flip(s, *offsets):
    data = bytearray(s.binfile.data)
    for o in offsets:
        data[o] ^= 0x5A
    return bytes(data)


@pytest.mark.parametrize("src", [EDC16, EDC15])
def test_a_sibling_has_the_same_model_without_matching(src, tmp_path):
    a = TunerSession(_need(src))
    data = _flip(a, 0x1000, len(a.binfile.data) // 2)
    sib = a.sibling(data)
    p = str(tmp_path / ("x" + os.path.splitext(src)[1]))
    with open(p, "wb") as fh:
        fh.write(data)
    _same_model(TunerSession(p), sib)
    assert sib.defn_state == "shared"
    assert bytes(sib.binfile.data) == data
    assert all(e.binfile is sib.binfile for e in sib.entities)
    assert all(e.binfile is a.binfile for e in a.entities)


def test_a_sibling_is_independent_of_the_session_it_came_from():
    a = TunerSession(_need(EDC16))
    before = bytes(a.binfile.data)
    sib = a.sibling(_flip(a, 0x2000))
    sib.binfile.data[0x3000] ^= 0xFF
    assert bytes(a.binfile.data) == before
    assert sib.undo_stack is not a.undo_stack and sib.change_log == []
    with pytest.raises(ValueError):
        a.sibling(before[:-1])


def test_a_zf_sibling_reads_its_own_cells_and_schedules(tmp_path):
    a = TunerSession(_need(ZF))
    sched = a.shift_schedules[0]
    off = sched.addr + 3
    data = _flip(a, off)
    sib = a.sibling(data)
    assert sib.defn_state == "shared"
    mine = sib.shift_schedules[0]
    assert mine is not sched and mine.binfile is sib.binfile
    cells = lambda m: [m.cell(r, c) for r in range(m.rows)
                       for c in range(m.cols)]
    assert cells(mine) != cells(sched), "reads the edited image"
    assert all(e.binfile is sib.binfile for e in sib.tcc_schedules)
    p = str(tmp_path / "z.bin")
    with open(p, "wb") as fh:
        fh.write(data)
    full = TunerSession(p)
    assert [e.name for e in full.entities] == [e.name for e in sib.entities]
    assert [m.addr for m in full.shift_schedules] == \
        [m.addr for m in sib.shift_schedules]


def test_a_history_version_reuses_the_loaded_definition():
    from ovtsuite import patch as P
    a = TunerSession(_need(EDC16))
    s = P._session_from_bytes(_flip(a, 0x1000), a)
    assert s.defn_state == "shared"


def test_sibling_verify_rejects_a_different_structure():
    a = TunerSession(_need(EDC16))
    with pytest.raises(ValueError):
        a.sibling(_flip(a, 0x1000), verify=True)


def test_a_6hp_container_opens_a_saved_version_without_matching(tmp_path):
    from ovtsuite import patch as P
    path = str(tmp_path / "v.6hp")
    s = TunerSession(_need(ZF))
    s.save(path, backup=False)
    s = TunerSession(path)
    assert s.defn_state == "used"
    m = s.shift_schedules[0]
    s.edit_bytes(m.addr, bytes([s.binfile.data[m.addr] ^ 0x10]))
    s.save(label="tweak")
    vid = H.list_versions(s)[0]["id"]
    ver = P.session_at_version(s, vid)
    assert ver.defn_state == "shared"
    assert ver.binfile.name.startswith("v.6hp @ ")


def test_a_6hp_version_pairs_every_schedule_with_the_loaded_file(tmp_path):
    from ovtsuite import patch as P
    path = str(tmp_path / "p.6hp")
    TunerSession(_need(ZF)).save(path, backup=False)
    s = TunerSession(path)
    m = s.shift_schedules[0]
    s.edit_bytes(m.addr, bytes([s.binfile.data[m.addr] ^ 0x10]))
    s.save(label="tweak")
    ver = P.session_at_version(s, H.list_versions(s)[0]["id"])
    assert ver.defn_state == "shared"
    for fam in ("shift_schedules", "tcc_schedules"):
        mine = getattr(s, fam)
        assert mine, fam
        paired = [s.counterpart(ver, e) for e in mine]
        assert all(p is not None for p in paired), fam
        assert all(p.binfile is ver.binfile for p in paired)
        back = [ver.counterpart(s, p) for p in paired]
        assert all(b is e for b, e in zip(back, mine)), fam


# -- what invalidates a stored definition -------------------------------------

#: ``MODEL_VERSION`` and the hash of ``defn.MODEL_FILES`` it was recorded with.
#: See test_the_model_files_are_unchanged_or_model_version_was_considered.
RECORDED_MODEL = (3, "b72ec1b3079a934926e9aa2088541f616f6f1df5")


def test_the_model_files_are_unchanged_or_model_version_was_considered():
    """A change to a file that builds the model (``defn.MODEL_FILES``) makes
    every stored definition stale in a source checkout -- but a FROZEN build
    tells builds apart only by ``MODEL_VERSION`` (and module versions). So
    this fails on any such change until the recorded hash is updated, which
    is the moment to decide whether the change alters what a model holds."""
    got = (D.MODEL_VERSION, D.model_files_digest())
    if not got[1]:
        pytest.skip("no source files (frozen build)")
    assert got == RECORDED_MODEL, (
        "a file in ovtsuite/defn.py MODEL_FILES changed (hash %s, recorded "
        "%s). If the change alters how a model is built or restored (what a "
        "loader derives, an entity field's meaning, the snapshot decode), "
        "bump defn.MODEL_VERSION. Either way set RECORDED_MODEL in "
        "tests/test_defn.py to (MODEL_VERSION, %r)."
        % (got[1], RECORDED_MODEL[1], got[1]))


def _fresh_digests(monkeypatch):
    monkeypatch.setattr(D, "_DIGEST_CACHE", {})


def test_the_code_digest_ignores_the_app_version_and_other_code(monkeypatch):
    from ovtsuite import branding
    _fresh_digests(monkeypatch)
    before = D.code_digest("edc16")
    monkeypatch.setattr(branding, "__version__", "99.9.9")
    _fresh_digests(monkeypatch)
    assert D.code_digest("edc16") == before
    # a base file outside MODEL_FILES (here: the GUI) is not hashed at all
    seen = []
    real = D._hash_source
    monkeypatch.setattr(D, "_hash_source",
                        lambda ps: seen.extend(ps) or real(ps))
    _fresh_digests(monkeypatch)
    D.code_digest("edc16")
    names = {os.path.basename(p) for p in seen}
    assert "session.py" in names and "match.py" in names
    assert not names & {"gui.py", "gui_project.py", "branding.py", "cli.py"}


def test_the_code_digest_follows_model_version_and_the_module(monkeypatch):
    from types import SimpleNamespace
    _fresh_digests(monkeypatch)
    before = D.code_digest("edc16")
    monkeypatch.setattr(D, "MODEL_VERSION", D.MODEL_VERSION + 1)
    _fresh_digests(monkeypatch)
    assert D.code_digest("edc16") != before
    monkeypatch.setattr(D, "MODEL_VERSION", D.MODEL_VERSION - 1)
    mod = SimpleNamespace(id="fake", version="1.0.0")
    monkeypatch.setattr(D, "_module_of", lambda p: mod)
    _fresh_digests(monkeypatch)
    v1 = D.code_digest("fake")
    mod.version = "1.0.1"
    _fresh_digests(monkeypatch)
    assert D.code_digest("fake") != v1


def test_a_frozen_build_digests_versions_only(monkeypatch, tmp_path):
    monkeypatch.setattr(D, "_PKG", str(tmp_path))       # no source files
    _fresh_digests(monkeypatch)
    assert D.model_files_digest() == ""
    a = D.code_digest(None)
    monkeypatch.setattr(D, "MODEL_VERSION", D.MODEL_VERSION + 1)
    _fresh_digests(monkeypatch)
    assert D.code_digest(None) != a


def test_the_data_digest_skips_the_search_index(monkeypatch, tmp_path):
    from ovtsuite import datafiles
    kb = tmp_path / "kb.json"
    idx = tmp_path / "search_index.json.gz"
    kb.write_bytes(b"kb")
    idx.write_bytes(b"one")
    monkeypatch.setattr(datafiles, "listing", lambda mid=None: [
        ("kb.json", str(kb)), ("search_index.json.gz", str(idx))])
    _fresh_digests(monkeypatch)
    a = D.data_digest()
    idx.write_bytes(b"two")
    _fresh_digests(monkeypatch)
    assert D.data_digest() == a
    kb.write_bytes(b"kb2")
    _fresh_digests(monkeypatch)
    assert D.data_digest() != a


def _installed(monkeypatch, version="1.0.11", variant="full"):
    """Make the edc16 module look installed from a package."""
    from types import SimpleNamespace
    from ovtsuite.modules import loader
    man = SimpleNamespace(id="edc16", version=version, variant=variant)
    monkeypatch.setattr(loader, "manifest",
                        lambda mid: man if mid == "edc16" else None)
    _fresh_digests(monkeypatch)
    return man


def test_an_installed_module_is_judged_by_its_version_only(monkeypatch, tmp_path):
    """Two computers with the same installer must agree: an installed
    module's files are not hashed (stray or extra files in its folder, its
    install location), only its id, version and variant."""
    from ovtsuite import datafiles
    from ovtsuite.modules import loader
    man = _installed(monkeypatch)
    hashed = []
    monkeypatch.setattr(D, "_hash_source", lambda ps: hashed.append(ps) or "x")
    monkeypatch.setattr(datafiles, "listing",
                        lambda mid=None: hashed.append(mid) or [])
    monkeypatch.setattr(D, "_module_of",
                        lambda p: __import__("types").SimpleNamespace(
                            id="edc16", version="1.0.11"))
    monkeypatch.setattr(loader, "module_roots",
                        lambda: {"edc16": str(tmp_path / "a")})
    code, data = D.code_digest("edc16"), D.data_digest("edc16")
    # the same module installed somewhere else, with other files beside it
    monkeypatch.setattr(loader, "module_roots",
                        lambda: {"edc16": str(tmp_path / "b")})
    _fresh_digests(monkeypatch)
    assert (D.code_digest("edc16"), D.data_digest("edc16")) == (code, data)
    # neither the module's source nor its data files were read
    assert "edc16" not in hashed
    assert not [f for h in hashed if isinstance(h, list) for f in h
                if str(tmp_path) in f]
    # ... and a new release of the module is a different model
    man.version = "1.0.12"
    _fresh_digests(monkeypatch)
    assert D.code_digest("edc16") != code
    assert D.data_digest("edc16") != data


def test_a_rejected_snapshot_says_which_code_differs(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(D, "_module_of", lambda p: SimpleNamespace(
        id="edc16", version="1.0.11"))
    _installed(monkeypatch, "1.0.10")
    snap = {"format": D.FORMAT, "platform": "edc16", "image_len": 4,
            "code": D.code_digest("edc16"),
            "code_parts": D.code_parts("edc16")}
    _installed(monkeypatch, "1.0.11")
    why = D.check(snap, "edc16", b"\0" * 4)
    assert why.startswith("built by different code")
    assert "edc16 1.0.10, here edc16 1.0.11" in why


def test_resetting_the_registry_forgets_the_digests():
    from ovtsuite.modules import registry
    D._DIGEST_CACHE["code|x"] = "stale"
    registry.reset()
    assert "code|x" not in D._DIGEST_CACHE


# -- locating parameters on a project's original version (defn.rebase) ----------

def _snap_of(data, ents, platform="edc16"):
    return json.dumps({
        "format": D.FORMAT, "platform": platform, "image_len": len(data),
        "code": D.code_digest(platform), "data": D.data_digest(platform),
        "mask": D.mask_digest(data, ents), "efields": D._EF, "afields": D._AF,
        "ents": [D._erow(e) for e in ents]}).encode()


def _one_map():
    from ovtsuite.model import Axis, Entity
    # header (2 count words) at 0x100, X axis 0x104..0x10C, Z 0x10C..0x114
    return Entity("M", "curve", 1, [], "", "", 0x10C, 1, 4, 16, False, "X",
                  "kb", x=Axis(0x104, 4, 16))


def test_rebase_carries_a_definition_to_an_edited_version():
    old = bytes(range(256)) * 4
    e = _one_map()
    new = bytearray(old)
    new[0x10C:0x114] = b"\xff" * 8          # map values edited
    new[0x3F0:0x3F4] = b"\x01\x02\x03\x04"  # a checksum far from any table
    snap = _snap_of(old, [e])
    assert D.usable(snap, "edc16", bytes(new)) is not None   # as stored: no
    out = D.rebase(snap, "edc16", old, bytes(new))
    assert out is not None
    assert D.usable(out, "edc16", bytes(new)) is None


def test_rebase_refuses_when_a_header_changed():
    old = bytes(range(256)) * 4
    e = _one_map()
    new = bytearray(old)
    new[0x101] = 6                          # the count word before the axis
    assert D.rebase(_snap_of(old, [e]), "edc16", old, bytes(new)) is None
    assert D.rebase(_snap_of(old, [e]), "edc16", old, bytes(new[:-1])) is None

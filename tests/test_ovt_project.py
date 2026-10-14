# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The .ovt project container (ovtsuite.project, docs/OVT_CONTAINER.md).

Synthetic tests first (fast, no reference data); real-file tests at the end
are skipped when the reference material is absent.
"""

import io
import json
import os
import random
import struct
import zipfile

import pytest

from ovtsuite import container as _c
from ovtsuite import project as ovt
from ovtsuite.project import archive as _ar
from ovtsuite.project import delta as _d

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF = os.path.join(ROOT, "Reference")


def _rand(n, seed):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(n))


def _edit(data, *pairs):
    b = bytearray(data)
    for off, val in pairs:
        b[off:off + len(val)] = val
    return bytes(b)


def _new_project():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    p.add_member(s.id, ovt.Member(id="eeprom", role="eeprom"))
    flash, ee = _rand(0x20000, 1), _rand(0x800, 2)
    root, stock = p.add_root(s.id, {"flash": flash, "eeprom": ee},
                             kind="read", label="Read from car")
    return p, s, root, stock, flash, ee


def _retamper(data, path, new_bytes):
    """Rewrite one archive entry, keeping the manifest (and its hashes)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            payload = new_bytes if info.filename == path else src.read(info)
            dst.writestr(info, payload)
    return out.getvalue()


def _rewrite_manifest(data, fn):
    src = zipfile.ZipFile(io.BytesIO(data))
    man = json.loads(src.read("manifest.json"))
    fn(man)
    raw = json.dumps(man).encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename == "manifest.json":
                dst.writestr(info, raw)
            elif info.filename == "integrity.json":
                dst.writestr(info, json.dumps(
                    {"manifest_sha256": _ar.sha256(raw), "signature": None}))
            else:
                dst.writestr(info, src.read(info))
    return out.getvalue()


# ------------------------------------------------------------------ deltas
def test_delta_round_trip_both_directions():
    a = {"flash": _rand(50000, 3), "eeprom": b"\x00" * 64}
    b = {"flash": _edit(a["flash"], (10, b"\xAA\xBB"), (20, b"\x01"),
                        (40000, b"xyz")),
         "mpc": b"\xFF" * 100}                     # eeprom gone, mpc new
    recs = _d.decode(_d.encode(_d.diff(a, b)))
    assert _d.apply_forward(a, recs) == b
    assert _d.apply_backward(b, recs) == a
    # 10..12 and 20 are < 16 bytes apart: one merged patch record
    patches = [r for r in recs if r.op == _d.OP_PATCH]
    assert len(patches) == 2


def test_delta_refuses_a_state_it_does_not_fit():
    a = {"flash": b"\x00" * 100}
    b = {"flash": _edit(a["flash"], (5, b"\x01"))}
    recs = _d.diff(a, b)
    with pytest.raises(_d.DeltaError):
        _d.apply_forward({"flash": b"\x02" * 100}, recs)


# ----------------------------------------------------------- identification
def test_is_project_is_structural(tmp_path):
    p, *_ = _new_project()
    data = p.to_bytes()
    assert ovt.is_project(data)
    plain_zip = io.BytesIO()
    with zipfile.ZipFile(plain_zip, "w") as z:
        z.writestr("mimetype", b"application/zip")
    assert not ovt.is_project(plain_zip.getvalue())
    assert not ovt.is_project(b"OVTEDC16" + b"\0" * 64)
    f = tmp_path / "x.ovt"
    f.write_bytes(data)
    assert ovt.is_project(str(f))


# ---------------------------------------------------------- history/branches
def test_commit_fork_and_reload(tmp_path):
    p, s, root, stock, flash, ee = _new_project()
    st1 = p.fork(s.id, "Stage 1")
    a = p.commit(s.id, st1.id, {"flash": _edit(flash, (0x100, b"\x11\x22"))},
                 label="boost")
    b = p.commit(s.id, st1.id, {"eeprom": _edit(ee, (4, b"\x99"))},
                 label="immo off")
    eco = p.fork(s.id, "Economy", at=root.id)
    c = p.commit(s.id, eco.id, {"flash": _edit(flash, (0x200, b"\x33"))})
    assert p.commit(s.id, eco.id, {"flash": p.member_bytes(s.id, "flash",
                                                           branch=eco.id)}) is None

    path = p.save(str(tmp_path / "car"))
    assert path.endswith(".ovt")
    q = ovt.Project.load(path)
    assert q.report.ok
    qs = q.set("ECU")
    assert [x.name for x in qs.branches] == ["Stock", "Stage 1", "Economy"]
    assert q.member_bytes(qs.id, "flash", branch="Stock") == flash
    head = q.head_state(qs.id, "Stage 1")
    assert head["flash"][0x100:0x102] == b"\x11\x22"
    assert head["eeprom"][4] == 0x99
    assert q.state(qs.id, a.id)["eeprom"] == ee            # mid-chain state
    assert q.state(qs.id, c.id)["flash"][0x200] == 0x33
    # the as-read original: root, never moved, still the read bytes
    assert qs.roots()[0].id == root.id and qs.checkpoint(root.id).kind == "read"
    assert q.state(qs.id, root.id) == {"flash": flash, "eeprom": ee}
    assert [x.id for x in qs.lineage(b.id)] == [b.id, a.id, root.id]


def test_only_roots_store_full_images():
    p, s, root, stock, flash, ee = _new_project()
    st1 = p.fork(s.id, "Stage 1")
    cur = flash
    for i in range(5):
        cur = _edit(cur, (i, b"\x5A"))
        p.commit(s.id, st1.id, {"flash": cur})
    other, ob = p.add_root(s.id, {"flash": _rand(0x100, 4)}, branch="OEM")
    q = ovt.Project.from_bytes(p.to_bytes())
    imgs = {k.split("/")[3] for k in q._blobs if "/images/" in k}
    assert imgs == {root.id, other.id}
    # heads are rebuilt from the root through the deltas
    assert q.head_state(s.id, "Stage 1")["flash"][:5] == b"\x5A" * 5


def test_revert_is_a_new_checkpoint():
    p, s, root, stock, flash, ee = _new_project()
    first = p.commit(s.id, None, {"flash": _edit(flash, (1, b"\x01"))})
    p.commit(s.id, None, {"flash": _edit(flash, (2, b"\x02"))})
    r = p.revert(s.id, None, first.id)
    assert r.kind == "revert" and r.parent != first.id
    assert p.head_state(s.id) == p.state(s.id, first.id)
    assert len(s.checkpoints) == 4


def test_independent_root_branch():
    p, s, root, stock, flash, ee = _new_project()
    other = _rand(0x20000, 9)
    c, b = p.add_root(s.id, {"flash": other}, kind="import",
                      label="stock SGO", branch="OEM stock")
    assert c.parent is None and len(s.roots()) == 2
    assert p.head_state(s.id, b.id) == {"flash": other}
    assert p.head_state(s.id, "Stock")["flash"] == flash


def test_last_branch_cannot_be_deleted_and_roots_survive():
    p, s, root, stock, flash, ee = _new_project()
    st1 = p.fork(s.id, "Stage 1")
    p.commit(s.id, st1.id, {"flash": _edit(flash, (9, b"\x09"))})
    p.delete_branch(s.id, "Stock")
    with pytest.raises(ValueError):
        p.delete_branch(s.id, "Stage 1")
    q = ovt.Project.from_bytes(p.to_bytes())
    assert q.state(q.sets[0].id, root.id)["flash"] == flash


# ----------------------------------------------------------------- integrity
def test_damaged_delta_loses_only_the_states_after_it():
    p, s, root, stock, flash, ee = _new_project()
    a = p.commit(s.id, None, {"flash": _edit(flash, (1, b"\x01"))})
    b = p.commit(s.id, None, {"flash": _edit(flash, (1, b"\x01"), (2, b"\x02"))})
    eco = p.fork(s.id, "Economy", at=a.id)
    e = p.commit(s.id, eco.id, {"flash": _edit(flash, (1, b"\x01"), (9, b"\x09"))})
    path = "sets/%s/deltas/%s.ovd" % (s.id, b.id)
    q = ovt.Project.from_bytes(_retamper(p.to_bytes(), path, b"garbage"))
    sid = q.sets[0].id
    assert path in q.report.damaged
    assert q.unreadable_checkpoints(sid) == [b.id]
    with pytest.raises(ovt.HistoryError):
        q.head_state(sid, "Stock")
    assert q.state(sid, a.id)["flash"][1] == 1                 # before it: fine
    assert q.head_state(sid, "Economy")["flash"][9] == 9      # other branch: fine
    again = ovt.Project.from_bytes(q.to_bytes())               # stays flagged
    assert path in again.report.damaged


def test_damaged_root_image_is_reported_not_rebuilt():
    p, s, root, stock, flash, ee = _new_project()
    p.commit(s.id, None, {"flash": _edit(flash, (7, b"\x77"))})
    path = "sets/%s/images/%s/flash.bin" % (s.id, root.id)
    bad = ovt.Project.from_bytes(_retamper(p.to_bytes(), path, b"\x00" * 0x20000))
    sid = bad.sets[0].id
    assert path in bad.report.damaged and not bad.report.ok
    assert set(bad.unreadable_checkpoints(sid)) == {c.id for c in s.checkpoints}
    again = ovt.Project.from_bytes(bad.to_bytes())
    assert path in again.report.damaged


def test_damage_is_never_laundered_by_a_resave():
    p, s, root, stock, flash, ee = _new_project()
    p.add_attachment("notes.txt", b"hello")
    data = p.to_bytes()
    att = next(k for k in p._blobs if k.startswith("attachments/"))
    q = ovt.Project.from_bytes(_retamper(data, att, b"HELLO"))
    assert att in q.report.damaged
    again = ovt.Project.from_bytes(q.to_bytes())
    assert att in again.report.damaged


def test_signing(tmp_path):
    pytest.importorskip("cryptography")
    from ovtsuite.modules import signing
    key = str(tmp_path / "k.pem")
    signing.generate_key(key)
    p, *_ = _new_project()
    data = p.to_bytes(sign_key=key, signer="Bear")
    q = ovt.Project.from_bytes(data)
    assert q.report.signature.status == "valid"
    assert q.report.signature.signer == "Bear"
    unsigned = ovt.Project.from_bytes(p.to_bytes())
    assert unsigned.report.signature.status == "unsigned"
    # change the manifest after signing: the signature no longer holds
    src = zipfile.ZipFile(io.BytesIO(data))
    integ = src.read("integrity.json")
    tampered = _rewrite_manifest(data, lambda m: m.update(name="evil"))
    t2 = _retamper(tampered, "integrity.json", integ)
    r = ovt.Project.from_bytes(t2).report
    assert r.signature.status == "invalid" and not r.ok


# ------------------------------------------------------ forward compatibility
def test_unknown_keys_and_entries_survive_a_round_trip():
    p, s, *_ = _new_project()
    data = p.to_bytes()

    def add(m):
        m["future_top"] = {"x": 1}
        m["sets"][0]["future_set"] = True
        m["sets"][0]["members"][0]["future_member"] = [1, 2]
    data = _rewrite_manifest(data, add)
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("future/thing.bin", b"\x01\x02")
    q = ovt.Project.from_bytes(buf.getvalue())
    out = ovt.Project.from_bytes(q.to_bytes())
    man = out.manifest()
    assert man["future_top"] == {"x": 1}
    assert man["sets"][0]["future_set"] is True
    assert man["sets"][0]["members"][0]["future_member"] == [1, 2]
    assert out._blobs["future/thing.bin"] == b"\x01\x02"


def test_unknown_critical_feature_is_refused():
    p, *_ = _new_project()
    data = _rewrite_manifest(p.to_bytes(),
                             lambda m: m["requires"].append("quantum-v9"))
    with pytest.raises(ovt.UnsupportedFeature) as ei:
        ovt.Project.from_bytes(data)
    assert "quantum-v9" in str(ei.value)


# ------------------------------------------------------------ logs/attachments
def test_log_cap_deletes_oldest_unpinned():
    p, s, *_ = _new_project()
    p.max_logs = 2
    a = p.add_log("a.csv", b"a", set_id=s.id)
    p.pin_log(a.id)
    b = p.add_log("b.csv", b"b")
    c = p.add_log("c.csv", b"c")
    d = p.add_log("d.csv", b"d")
    assert [x.filename for x in p.logs] == ["a.csv", "c.csv", "d.csv"]
    assert not any(k.endswith("/b.csv") for k in p._blobs)
    q = ovt.Project.from_bytes(p.to_bytes())
    assert q.max_logs == 2 and q.logs[0].pinned
    assert q.log_bytes(d.id) == b"d"
    p.pin_log(a.id, False)                     # now it counts, and is oldest
    assert [x.filename for x in p.logs] == ["c.csv", "d.csv"]


# ------------------------------------------------------- import / definitions
def test_raw_import_packages_a_definition_beside_it(tmp_path):
    raw = _rand(0x8000, 11)
    (tmp_path / "unknown.bin").write_bytes(raw)
    (tmp_path / "unknown.xdf").write_bytes(b"<XDFFORMAT/>")
    proj, s = ovt.import_file(str(tmp_path / "unknown.bin"))
    m = s.member("flash")
    assert m.role == "raw" and s.platform == "unknown"
    assert m.definition["type"] == "xdf"
    q = ovt.Project.from_bytes(proj.to_bytes())
    assert q.definition_bytes(q.sets[0].id, "flash") == b"<XDFFORMAT/>"
    assert q.member_bytes(q.sets[0].id, "flash") == raw


def _version_line(rec):
    return "  VERSION " + json.dumps(rec, separators=(",", ":"))


def test_legacy_container_history_becomes_checkpoints():
    v0 = _rand(0x4000, 21)
    ee0 = b"\x00" * 32
    v1 = _edit(v0, (0x10, b"\xA1"))
    v2 = _edit(v1, (0x20, b"\xB2\xB3"))
    ee2 = _edit(ee0, (3, b"\x7E"))
    log = "\n".join([
        "# 2026-10-01 10:00:00  saved t.edc16",
        _version_line({"id": "one", "ts": "2026-10-01T10:00:00", "label": "save",
                       "ranges": [[0x10, v0[0x10:0x11].hex(), "a1"]]}),
        "  PATCH {\"source\":\"x\"}",
        "# 2026-10-02 10:00:00  saved t.edc16",
        _version_line({"id": "two", "ts": "2026-10-02T10:00:00", "label": "more",
                       "ranges": [[0x20, v1[0x20:0x22].hex(), "b2b3"]],
                       "regions": {"EEPROM": [[3, "00", "7e"]]}}),
    ])
    cont = _c.Container("edc16")
    cont.add(_c.BLOCK_EXTERNAL, v2)
    cont.add(_c.BLOCK_EEPROM, ee2)
    cont.add(_c.BLOCK_LOG, log.encode())
    proj = ovt.Project("legacy")
    s = ovt.import_bytes(proj, cont.to_bytes(), "t.edc16")
    chain = list(reversed(s.lineage(proj.head(s.id).id)))
    assert len(chain) == 3
    assert proj.state(s.id, chain[0].id) == {"flash": v0, "eeprom": ee0}
    assert proj.state(s.id, chain[1].id) == {"flash": v1, "eeprom": ee0}
    assert proj.head_state(s.id) == {"flash": v2, "eeprom": ee2}
    assert chain[2].ts == "2026-10-02T10:00:00"
    # the PATCH record stays with the save it belongs to (Remove patch reads
    # it there); nothing is left over for an attachment
    assert chain[1].kind == "patch" and chain[1].log == ['PATCH {"source":"x"}']
    assert chain[2].label == "more"
    assert not any(a.filename == "legacy-history.log" for a in proj.attachments)


def test_history_text_round_trips_through_the_session_log():
    """A project branch written as change-log text reads back as the same
    versions, and a save appended to it is what read_log reports as new."""
    from ovtsuite.project import sessionbridge as sb
    p, s, root, stock, flash, ee = _new_project()
    a = p.commit(s.id, None, {"flash": _edit(flash, (1, b"\x01"))},
                 log=['PATCH {"source":"stage1.patch"}'], kind="patch",
                 label="patch: stage1.patch")
    b = p.commit(s.id, None, {"eeprom": _edit(ee, (2, b"\x02"))})
    text = sb.history_text(p, s.id, b.id)
    import json as _json
    recs = [_json.loads(l.strip()[8:]) for l in text.split("\n")
            if l.strip().startswith("VERSION ")]
    assert [r["id"] for r in recs] == [a.id, b.id]
    assert recs[1]["regions"] == {"EEPROM": [[2, ee[2:3].hex(), "02"]]}
    assert 'PATCH {"source":"stage1.patch"}' in text
    renamed = text.replace('"id":"%s"' % a.id, '"id":"%s","name":"Stage 1"' % a.id)
    later = renamed + "# 2026-10-11 12:00:00  saved x\n  bytes @0x5: 1 byte(s)\n" \
        '  VERSION {"id":"zzz","ranges":[[5,"00","01"]]}\n'
    new, label, renames = sb.read_log(p, s.id, b.id, later)
    assert new == ["bytes @0x5: 1 byte(s)"] and label == ""
    assert renames == {a.id: "Stage 1"}


def test_legacy_chain_stops_at_a_record_that_does_not_fit():
    v0 = b"\x00" * 64
    v1 = _edit(v0, (1, b"\x01"))
    log = "\n".join([
        _version_line({"id": "bad", "ranges": [[5, "00", "55"]]}),  # drifted
        _version_line({"id": "ok", "ranges": [[1, "00", "01"]]}),
    ])
    chain = ovt.legacy_chain({"flash": v1}, log)
    assert [r.get("id") for r, _ in chain] == [None, "ok"]
    assert chain[0][1]["flash"] == v0


def test_export_raw_and_no_legacy_writer():
    """The older native containers (.edc16 / .6hp / ...) are import-only."""
    p, s, root, stock, flash, ee = _new_project()
    s.platform = "edc16"
    assert ovt.export_raw(p, s.id, "eeprom") == ee
    assert not hasattr(ovt, "export_legacy")


# ------------------------------------------------------------ real reference
_DIM = os.path.join(REF, "Binaries", "Encoded-MPC", "stock.dim")
_TUNE = os.path.join(REF, "Tunes", "6hp19-tdi20-FULL-HISTORY-33-checkpoints.6hp")
_09D_SGO = os.path.join(REF, "Aisin09D", "SGO", "09D927750HN_3403.sgo")
_FRF = os.path.join(REF, "ZF6HP", "OEM", "6HP28", "containers",
                    "FL_8K0927158A__0050.frf")


@pytest.mark.skipif(not os.path.exists(_DIM), reason="reference .dim absent")
def test_real_dim_imports_and_exports_losslessly():
    from ovtsuite import dim as _dim
    proj, s = ovt.import_file(_DIM)
    assert {m.id for m in s.members} >= {"flash", "mpc", "eeprom"}
    o = s.originals[0]
    with open(_DIM, "rb") as fh:
        raw = fh.read()
    assert ovt.export_original(proj, s.id, o.id) == raw          # untouched
    flash = proj.member_bytes(s.id, "flash")
    edited = _edit(flash, (0x1C0000, b"\x12\x34"))
    proj.commit(s.id, None, {"flash": edited})
    rebuilt = _dim.load(ovt.export_original(proj, s.id, o.id))
    assert _dim.external_flash(rebuilt) == edited
    assert _dim.internal_flash(rebuilt) == proj.member_bytes(s.id, "mpc")


@pytest.mark.skipif(not os.path.exists(_TUNE), reason="reference tune absent")
def test_real_6hp_tune_history_migrates():
    with open(_TUNE, "rb") as fh:
        raw = fh.read()
    cont = _c.Container.from_bytes(raw)
    proj, s = ovt.import_file(_TUNE)
    assert s.platform == "zf6hp"
    assert proj.head_state(s.id)["flash"] == cont.external()
    n_versions = sum(1 for ln in bytes(cont.get(_c.BLOCK_LOG).data)
                     .decode("utf-8", "replace").split("\n")
                     if ln.strip().startswith("VERSION "))
    depth = len(s.lineage(proj.head(s.id).id))
    assert depth >= 2
    assert depth - 1 <= n_versions + 1
    q = ovt.Project.from_bytes(proj.to_bytes())
    qs = q.sets[0]
    for c in qs.checkpoints:                       # every state rebuilds
        q.state(qs.id, c.id)


@pytest.mark.skipif(not os.path.exists(_09D_SGO), reason="reference .sgo absent")
def test_real_sgo_original_round_trips_unchanged():
    proj, s = ovt.import_file(_09D_SGO)
    with open(_09D_SGO, "rb") as fh:
        raw = fh.read()
    assert ovt.export_original(proj, s.id, s.originals[0].id) == raw


@pytest.mark.skipif(not os.path.exists(_FRF), reason="reference .frf absent")
def test_real_frf_imports_with_original():
    proj, s = ovt.import_file(_FRF)
    assert s.platform == "zf6hp"
    assert s.originals[0].format == "frf"
    flash = proj.member_bytes(s.id, "flash")
    proj.commit(s.id, None, {"flash": _edit(flash, (0x100, bytes([flash[0x100] ^ 1])))})
    with pytest.raises(ovt.ExportRefused):            # .frf has no writer
        ovt.export_original(proj, s.id, s.originals[0].id)


# ------------------------------------------------------------ crash recovery
from ovtsuite.project import autosave as _as


def _crash(saver):
    """Simulate the owning process dying: its lock goes, the files stay."""
    saver.flush()
    if saver._lock is not None:
        saver._lock.release()
        saver._lock = None


def test_autosave_recovers_raw_unsaved_edits(tmp_path):
    root = str(tmp_path / "rec")
    p, s, r, stock, flash, ee = _new_project()
    p.save(str(tmp_path / "car.ovt"))
    saver = _as.AutoSaver(p, root)
    edited = _edit(flash, (0x40, b"\xDE\xAD"))        # checksum NOT corrected
    assert saver.maybe_snapshot(lambda: {(s.id, stock.id): {"flash": edited}},
                                token=1)
    saver.flush()
    assert not saver.maybe_snapshot(lambda: {}, token=1)    # unchanged: skipped
    assert _as.pending(root) == []                  # owner alive: not offered
    _crash(saver)
    recs = _as.pending(root)
    assert len(recs) == 1 and "ECU / Stock" in recs[0].describe()
    assert not os.path.exists(os.path.join(recs[0].dir, _as.BASE))
    proj, working, warns = _as.recover(recs[0])
    assert warns == []
    assert proj.path == os.path.abspath(str(tmp_path / "car.ovt"))
    got = working[(s.id, stock.id)]
    assert got["flash"] == edited and got["eeprom"] == ee
    # nothing was committed: the project on disk is untouched
    assert proj.head_state(s.id)["flash"] == flash
    _as.discard(recs[0])
    assert _as.pending(root) == []


def test_autosave_untitled_project_keeps_a_base_copy(tmp_path):
    root = str(tmp_path / "rec")
    p, s, r, stock, flash, ee = _new_project()       # never saved
    st1 = p.fork(s.id, "Stage 1")
    saver = _as.AutoSaver(p, root)
    saver.snapshot({(s.id, st1.id): {"eeprom": _edit(ee, (1, b"\x01"))}},
                   wait=True)
    _crash(saver)
    rec = _as.pending(root)[0]
    proj, working, _w = _as.recover(rec)
    assert proj.path is None and proj.modified_since_save
    assert [b.name for b in proj.set("ECU").branches] == ["Stock", "Stage 1"]
    assert working[(s.id, st1.id)]["eeprom"][1] == 1


def test_proper_exit_or_save_discards_recovery(tmp_path):
    root = str(tmp_path / "rec")
    p, s, r, stock, flash, ee = _new_project()
    p.save(str(tmp_path / "car.ovt"))
    saver = _as.AutoSaver(p, root)
    saver.snapshot({(s.id, stock.id): {"flash": _edit(flash, (0, b"\x00"))}},
                   wait=True)
    assert saver.active
    saver.discard()                       # exit, user refused to save
    assert not saver.active and _as.pending(root) == []
    # after a save the working bytes equal the head: nothing is written
    saver.snapshot({(s.id, stock.id): {"flash": flash}}, wait=True)
    assert not saver.active


# ------------------------------------------------------- project information
def test_project_information_round_trips_and_keeps_unknown_fields():
    p = ovt.Project("car")
    assert p.set_info({"make": "VW", "model": " Golf ", "vin": "", "Fuel": "Diesel"})
    assert p.info == {"make": "VW", "model": "Golf", "Fuel": "Diesel"}
    assert not p.set_info({"make": "VW"})                  # nothing changed
    assert p.vehicle() == "VW Golf"
    p.set_info({"chassis": "1K", "year": "2006"})
    assert p.vehicle() == "VW Golf (1K) 2006"
    q = ovt.Project.from_bytes(p.to_bytes())
    assert q.info == p.info
    rows = dict(q.info_rows())
    assert rows["Make"] == "VW" and rows["Fuel"] == "Diesel"
    assert "Model: Golf" in q.summary()
    p.set_info({"model": ""})                              # empty removes
    assert "model" not in p.info
    p.set_info({"make": "Audi"}, replace=True)
    assert p.info == {"make": "Audi"}


def test_old_formats_are_imported_not_refused():
    """.edc15bak and the first .hvac layout are import-only: each becomes a
    set (flash + EEPROM members for the backup) -- nothing writes them."""
    from ovtsuite.flash import backup15
    from ovtsuite.flash import hvac_image as H
    from ovtsuite.project import importers
    flash, eeprom = _rand(0x8000, 3), _rand(0x200, 4)
    proj = ovt.Project()
    s = importers.import_bytes(proj, backup15.build(flash, eeprom), "car.edc15bak")
    st = proj.head_state(s.id)
    assert st["flash"] == flash and st["eeprom"] == eeprom
    assert s.member("eeprom").role == "eeprom"
    hi = H.HvacImage.from_image(b"\x5A" * 0x40000, loader=b"\x00" * 0x2A2,
                                base=0xFC0000, part="4F0910043B")
    proj = ovt.Project()
    s = importers.import_bytes(proj, hi.to_own(fix_checksum=False), "old.hvac")
    st = proj.head_state(s.id)
    assert st["flash"] == bytes(hi.image) and st["loader"] == hi.loader


# ---------------------------------------------------- Climatronic .sgo
def _hvac_sgo(fill=0x5A, name="4F0910043B__0080.sgo"):
    base = 0xFC0000
    secs = [(base, bytes([0x11]) * 0x2A2)]          # loader inside section 1
    for i, (o, l) in enumerate(((0, 0x10000), (0x10000, 0x10000),
                                (0x20000, 0x10000), (0x30000, 0x8000),
                                (0x38000, 0x2000))):
        secs.append((base + o, bytes([(fill + i) & 0xFF]) * l))
    pre = bytearray(0x80)
    pre[:16] = b"SGML Object File"
    fn = name.encode().ljust(40, b"\x00")
    pre[0x31:0x31 + 40] = bytes(b ^ 0xFF for b in fn)
    ms = 0x60
    struct.pack_into("<I", pre, 0x29, ms)
    struct.pack_into("<I", pre, ms, 0)               # metadata length 0
    first = ms + 0 + 4
    pre = pre[:first]
    out = bytearray(pre)
    offs = []
    for a, d in secs:
        offs.append(len(out))
        h = bytearray(0x19)
        h[0:3] = struct.pack(">I", a)[1:]
        h[3] = 0
        h[4:7] = struct.pack(">I", len(d))[1:]
        h[0x0D:0x10] = struct.pack(">I", a)[1:]
        h[0x10:0x13] = struct.pack(">I", a + len(d) - 1)[1:]
        struct.pack_into(">H", h, 0x13, sum(d) & 0xFFFF)
        struct.pack_into("<I", h, 0x15, len(d))
        out += h + bytes(b ^ 0xFF for b in d)
    sgolen = len(out)
    struct.pack_into("<I", out, 0x2D, sgolen)
    tr = struct.pack("<I", len(offs)) + b"".join(struct.pack("<I", o) for o in offs)
    out += tr
    from ovtsuite import sgo
    return sgo.to_bytes(sgo.parse(bytes(out), name))


def test_a_climatronic_sgo_imports_and_exports_with_its_loader(tmp_path):
    """Its sections sit at 0xFC0000+, past the 0-based image other .sgo files
    flatten into: it used to import as "no section decodes". The flash
    loader is a member of its own; an edited version re-packs into the
    .sgo with the loader carried through, and the stock section digests let
    the write skip what was not changed."""
    from ovtsuite.flash import hvac_image as H
    from ovtsuite.project import importers, exporters
    from ovtsuite.project import sessionbridge as sb
    from ovtsuite.session import TunerSession
    raw = _hvac_sgo()
    p = ovt.Project()
    s = importers.import_bytes(p, raw, "4F0910043B__0080.sgo")
    assert s.platform == "hvac"
    st = p.head_state(s.id)
    assert len(st["flash"]) == 0x3A000 and len(st["loader"]) == 0x2A2
    assert s.member("flash").base == 0xFC0000
    o = s.originals[0]
    assert exporters.export_original(p, s.id, o.id) == raw      # untouched
    fl = bytearray(st["flash"])
    fl[0x100] ^= 0xFF
    p.commit(s.id, None, dict(st, flash=bytes(fl)), label="edit")
    out = H.HvacImage.from_sgo(exporters.export_original(p, s.id, o.id))
    assert out.image == fl and out.loader == st["loader"]
    ext, blob = sb.working_file(p, s.id, p.head_state(s.id))
    ts = TunerSession(sb.write_working_file(str(tmp_path), s.name, ext, blob))
    assert ts._hvac.baseline_trusted and ts._hvac.part == "4F0910043B"
    assert ts._hvac.changed_section_addrs() == {0xFC0000}


def test_merging_versions_keeps_the_newest_and_its_bytes():
    p, s, root, stock, flash, ee = _new_project()
    st = p.head_state(s.id)
    states = []
    for i in range(3):
        f = bytearray(st["flash"])
        f[100 + i] ^= 0xFF
        st = dict(st, flash=bytes(f))
        states.append(st)
        p.commit(s.id, None, st, label="edit %d" % i, log=["line %d" % i])
    chain = [c.id for c in s.lineage(p.head(s.id).id)][:3]   # newest first
    head = chain[0]
    m = p.merge_checkpoints(s.id, chain[:2], label="both")
    assert m.id == head and m.label == "both" and m.kind == "merge"
    assert m.parent == chain[2] and m.log == ["line 1", "line 2"]
    assert p.state(s.id, head) == states[2]
    q = ovt.Project.from_bytes(p.to_bytes())
    assert q.state(s.id, head) == states[2]
    assert [c.id for c in q.set(s.id).lineage(head)][:2] == [head, chain[2]]
    with pytest.raises(ValueError):
        p.merge_checkpoints(s.id, [head, root.id])         # the original
    b = p.fork(s.id, "Side", at=chain[2])
    with pytest.raises(ValueError):
        p.merge_checkpoints(s.id, [head, chain[2]])         # a branch starts there


# -- definition cache: forks and sibling branches ------------------------------

def test_a_fork_takes_its_source_branchs_definition_cache():
    p, s, root, stock, flash, ee = _new_project()
    p.set_cache(s.id, stock.id, b"DEFN-stock")
    b = p.fork(s.id, "Stage 1")
    assert p.cache(s.id, b.id) == b"DEFN-stock"
    # a fork of a version the active branch is not at still gets one
    p.commit(s.id, None, {"flash": _edit(flash, (0x10, b"\x01"))}, label="x")
    c = p.fork(s.id, "Side", at=root.id)
    assert p.cache(s.id, c.id) == b"DEFN-stock"
    # it survives a round trip, and deleting the branch drops it
    q = ovt.Project.from_bytes(p.to_bytes())
    assert q.cache(s.id, c.id) == b"DEFN-stock"
    q.delete_branch(s.id, c.id)
    assert not any(k.endswith("/%s.defn" % c.id) for k in q._blobs)


def test_a_branch_without_a_cache_falls_back_to_a_siblings():
    p, s, root, stock, flash, ee = _new_project()
    a = p.fork(s.id, "A")
    b = p.fork(s.id, "B")
    assert p.cache(s.id, a.id) is None and p.best_cache(s.id, a.id) is None
    p.commit(s.id, a.id, {"flash": _edit(flash, (1, b"\x01"))}, label="a")
    p.commit(s.id, b.id, {"flash": _edit(flash, (2, b"\x02"))}, label="b")
    s.checkpoint(a.head).ts = "2026-01-01T00:00:00"
    s.checkpoint(b.head).ts = "2026-02-01T00:00:00"
    p.set_cache(s.id, a.id, b"DEFN-a")
    p.set_cache(s.id, b.id, b"DEFN-b")
    assert p.best_cache(s.id, a.id) == b"DEFN-a"           # its own first
    # stock has none: the most recently saved sibling's, then the other
    assert p.cache_candidates(s.id, stock.id) == [b"DEFN-b", b"DEFN-a"]
    assert p.best_cache(s.id, stock.id) == b"DEFN-b"


def test_the_working_file_definition_skips_a_rejected_one(monkeypatch):
    from ovtsuite import defn as D
    from ovtsuite.project import sessionbridge as sb
    p, s, root, stock, flash, ee = _new_project()
    other = p.fork(s.id, "Other")
    state = p.head_state(s.id)
    assert sb.definition_for(p, s.id, stock.id, state) is None
    p.set_cache(s.id, stock.id, b"own")
    # one candidate: passed on unchecked (the session decides)
    monkeypatch.setattr(D, "usable", lambda *a: pytest.fail("checked"))
    assert sb.definition_for(p, s.id, stock.id, state) == b"own"
    p.set_cache(s.id, other.id, b"sibling")
    seen = []

    def usable(blob, platform, data):
        seen.append((blob, platform, data))
        return "image structure changed" if blob == b"own" else None
    monkeypatch.setattr(D, "usable", usable)
    assert sb.definition_for(p, s.id, stock.id, state) == b"sibling"
    assert seen[0] == (b"own", "unknown", flash)
    # none usable: its own goes in, and the session says why it was not used
    monkeypatch.setattr(D, "usable", lambda *a: "built by different code")
    assert sb.definition_for(p, s.id, stock.id, state) == b"own"

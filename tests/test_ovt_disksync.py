# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Another program changed the .ovt while it was open (ovtsuite.project.disksync).

Synthetic, fast: a save refuses to overwrite a changed file, and the three-way
merge of the project in memory with the file as it is now keeps both sides'
work (new versions, branches, logs, information), puts ours on a "(mine)"
branch when both saved to the same branch, reports conflicting edits, and
keeps the history rebuildable when the other side merged versions away.
"""

import os
import random

import pytest

from ovtsuite import project as ovt
from ovtsuite.project import disksync as _ds


def _rand(n, seed):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(n))


def _edit(data, off, val):
    b = bytearray(data)
    b[off:off + len(val)] = val
    return bytes(b)


@pytest.fixture
def car(tmp_path):
    """A saved project: one set, Stock + Stage 1 (two versions on it)."""
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    flash = _rand(0x8000, 1)
    p.add_root(s.id, {"flash": flash}, kind="read", label="Read")
    st1 = p.fork(s.id, "Stage 1")
    p.commit(s.id, st1.id, {"flash": _edit(flash, 0x100, b"\x01")}, label="a")
    p.commit(s.id, st1.id, {"flash": _edit(_edit(flash, 0x100, b"\x01"),
                                           0x200, b"\x02")}, label="b")
    path = str(tmp_path / "car.ovt")
    p.save(path)
    return path


def _other_program(path, fn):
    """Load, change and save the file the way another tool would."""
    q = ovt.Project.load(path)
    fn(q)
    q.save(path)
    return q


def _flash(p, branch):
    s = p.set("ECU")
    return p.member_bytes(s.id, "flash", branch=branch)


# ------------------------------------------------------------- detection
def test_save_refuses_a_file_another_program_changed(car):
    mine = ovt.Project.load(car)
    assert mine.changed_on_disk() is None
    _other_program(car, lambda q: q.set_info({"vin": "WVWZZZ1KZ6W000001"}))
    assert mine.changed_on_disk() == "changed"
    mine.set_info({"make": "VW"})
    with pytest.raises(_ds.DiskChanged):
        mine.save()
    assert ovt.Project.load(car).info.get("vin")         # nothing written
    mine.save(force=True)                                # Overwrite
    assert "vin" not in ovt.Project.load(car).info
    assert mine.changed_on_disk() is None                # ours now


def test_a_touched_file_or_a_save_elsewhere_is_not_a_change(car, tmp_path):
    mine = ovt.Project.load(car)
    st = os.stat(car)
    os.utime(car, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert mine.changed_on_disk(quick=True) == "changed"   # time only...
    assert mine.changed_on_disk() is None                  # ...same content
    _other_program(car, lambda q: q.set_info({"year": "2006"}))
    mine.save(str(tmp_path / "copy.ovt"))                  # Save as: not checked
    assert mine.changed_on_disk() is None                  # follows the copy


def test_a_deleted_file_is_reported(car):
    mine = ovt.Project.load(car)
    os.remove(car)
    assert mine.changed_on_disk() == "deleted"
    with pytest.raises(_ds.DiskChanged) as ei:
        mine.save()
    assert ei.value.deleted


# ---------------------------------------------------------------- merging
def test_both_sides_additions_are_kept(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    base = _flash(mine, "Stage 1")
    mine.commit(s.id, "Stage 1", {"flash": _edit(base, 0x300, b"\x03")},
                label="mine")
    mine.add_log("run1.csv", b"t,rpm\n0,800\n")
    mine.set_info({"make": "VW"})

    def theirs(q):
        qs = q.set("ECU")
        eco = q.fork(qs.id, "Economy", at=qs.roots()[0].id)
        q.commit(qs.id, eco.id, {"flash": _edit(_flash(q, "Stock"), 0x400,
                                                b"\x04")}, label="eco")
        q.add_attachment("notes.txt", b"hello")
        q.set_info({"vin": "WVWZZZ1KZ6W000001"})
    _other_program(car, theirs)

    res = _ds.merge(mine, ovt.Project.load(car))
    assert res.conflicts == [] and res.differs
    p = res.project
    assert [b.name for b in p.set("ECU").branches] == ["Stock", "Stage 1",
                                                       "Economy"]
    assert _flash(p, "Stage 1")[0x300] == 3                # our new version
    assert _flash(p, "Economy")[0x400] == 4                # theirs
    assert [a.filename for a in p.logs] == ["run1.csv"]
    assert [a.filename for a in p.attachments] == ["notes.txt"]
    assert p.info == {"vin": "WVWZZZ1KZ6W000001", "make": "VW"}
    p.save()                                               # its base is the file now
    q = ovt.Project.load(car)
    assert q.report.ok and not q.unreadable_checkpoints(q.set("ECU").id)
    assert q.log_bytes(q.logs[0].id) == b"t,rpm\n0,800\n"


def test_a_branch_both_saved_to_keeps_theirs_and_puts_ours_beside_it(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    bid = s.branch("Stage 1").id
    mine.commit(s.id, bid, {"flash": _edit(_flash(mine, bid), 0x10, b"\xAA")},
                label="mine")
    _other_program(car, lambda q: q.commit(
        q.set("ECU").id, "Stage 1",
        {"flash": _edit(_flash(q, "Stage 1"), 0x20, b"\xBB")}, label="theirs"))
    res = _ds.merge(mine, ovt.Project.load(car))
    p = res.project
    new = res.moved[(s.id, bid)]
    assert p.set("ECU").branch(new).name == "Stage 1 (mine)"
    assert _flash(p, bid)[0x20] == 0xBB and _flash(p, bid)[0x10] != 0xAA
    assert _flash(p, new)[0x10] == 0xAA and _flash(p, new)[0x20] != 0xBB
    assert any("Stage 1 (mine)" in n for n in res.notes)


def test_conflicting_edits_are_reported_and_resolved_by_choice(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    cid = s.branch("Stage 1").head
    mine.set_checkpoint_label(s.id, cid, "my name")
    mine.set_info({"make": "VW"})
    _other_program(car, lambda q: (
        q.set_checkpoint_label(q.set("ECU").id, cid, "their name"),
        q.set_info({"make": "Audi"})))
    theirs = ovt.Project.load(car)
    res = _ds.merge(mine, theirs)
    keys = {c.key for c in res.conflicts}
    assert keys == {("sets", s.id, "checkpoints", cid, "label"), ("info", "make")}
    assert res.project.info["make"] == "Audi"            # default: theirs
    label = next(c for c in res.conflicts if c.key[0] == "sets").label
    assert "ECU" in label and "version" in label
    res = _ds.merge(mine, theirs, {("info", "make"): "mine"})
    assert res.project.info["make"] == "VW"
    assert res.project.set("ECU").checkpoint(cid).label == "their name"


def test_deletions_apply_unless_the_branch_is_still_being_edited(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    bid = s.branch("Stage 1").id
    _other_program(car, lambda q: q.delete_branch(q.set("ECU").id, "Stage 1"))
    theirs = ovt.Project.load(car)
    assert _ds.merge(mine, theirs).project.set("ECU").branch(bid) is None
    res = _ds.merge(mine, theirs, keep_branches=[(s.id, bid)])
    assert res.project.set("ECU").branch(bid) is not None
    assert res.notes


def test_our_version_on_top_of_versions_they_merged_away(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    stage = s.branch("Stage 1")
    a, b = [c.id for c in reversed(s.lineage(stage.head))][1:3]
    # we fork at "a" and save on it; they merge "a" and "b" into one
    side = mine.fork(s.id, "Side", at=a)
    want = _edit(mine.state(s.id, a)["flash"], 0x500, b"\x05")
    mine.commit(s.id, side.id, {"flash": want}, label="side")
    _other_program(car, lambda q: q.merge_checkpoints(q.set("ECU").id, [a, b]))
    res = _ds.merge(mine, ovt.Project.load(car))
    p = res.project
    ps = p.set("ECU")
    assert ps.checkpoint(a) is None                      # theirs: merged away
    assert _flash(p, "Side") == want                     # ours: still exact
    assert not p.unreadable_checkpoints(ps.id)
    p.save()
    assert ovt.Project.load(car).member_bytes(ps.id, "flash", branch="Side") == want


def test_nothing_changed_here_is_just_the_file(car):
    mine = ovt.Project.load(car)
    _other_program(car, lambda q: q.add_log("x.csv", b"1"))
    res = _ds.merge(mine, ovt.Project.load(car))
    assert not res.differs and not res.project.modified_since_save
    assert res.project.changed_on_disk() is None


def test_without_a_known_base_both_sides_are_unioned(car):
    mine = ovt.Project.load(car)
    mine._disk = None                                    # base unknown
    mine.add_log("mine.csv", b"m")
    _other_program(car, lambda q: q.add_log("theirs.csv", b"t"))
    res = _ds.merge(mine, ovt.Project.load(car))
    assert sorted(a.filename for a in res.project.logs) == ["mine.csv",
                                                           "theirs.csv"]


def test_a_definition_both_replaced_is_a_conflict(car):
    mine = ovt.Project.load(car)
    sid = mine.set("ECU").id
    mine.add_definition(sid, "flash", "ecu.xdf", b"<mine/>")
    _other_program(car, lambda q: q.add_definition(sid, "flash", "ecu.xdf",
                                                   b"<theirs/>"))
    theirs = ovt.Project.load(car)
    res = _ds.merge(mine, theirs)
    assert [c.key[0] for c in res.conflicts] == ["blob"]
    assert res.project.definition_bytes(sid, "flash") == b"<theirs/>"
    key = res.conflicts[0].key
    res = _ds.merge(mine, theirs, {key: "mine"})
    assert res.project.definition_bytes(sid, "flash") == b"<mine/>"


def test_a_definition_cache_only_we_have_is_kept_but_not_unsaved(car):
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    mine.set_cache(s.id, "Stage 1", b"DEFN")
    _other_program(car, lambda q: q.add_log("x.csv", b"1"))
    res = _ds.merge(mine, ovt.Project.load(car))
    assert not res.differs and not res.project.modified_since_save
    assert res.project.cache(s.id, "Stage 1") == b"DEFN"


# ------------------------------------------- definition caches on their own
def test_a_rebuilt_cache_goes_into_the_file_without_the_other_edits(car):
    from ovtsuite.project import cachewrite as _cw
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    mine.set_info({"make": "VW"})                   # an unsaved edit
    mine.add_log("run.csv", b"1,2")                 # and another
    mine.set_cache(s.id, "Stage 1", b"DEFN-stage1")
    before = ovt.Project.load(car)
    job = _cw.prepare(mine)
    assert job is not None and job.run() and job.outcome == "written"
    q = ovt.Project.load(car)
    assert q.cache(s.id, "Stage 1") == b"DEFN-stage1"
    assert "make" not in q.info and not q.logs      # nothing else went in
    assert q.modified == before.modified
    assert q.manifest()["sets"] == before.manifest()["sets"]
    # the other edits are still unsaved, and the next save is not refused
    assert mine.modified_since_save
    assert mine.changed_on_disk() is None
    assert _cw.persisted(mine) and _cw.prepare(mine) is None
    mine.save()
    q = ovt.Project.load(car)
    assert q.info["make"] == "VW" and q.cache(s.id, "Stage 1") == b"DEFN-stage1"
    assert not mine.modified_since_save


def test_a_cache_write_copies_the_other_entries_as_stored(car):
    import zipfile
    from ovtsuite.project import cachewrite as _cw
    mine = ovt.Project.load(car)
    mine.set_cache(mine.set("ECU").id, "Stock", b"DEFN" * 100)
    with zipfile.ZipFile(car) as zf:
        old = {i.filename: (i.CRC, i.compress_size) for i in zf.infolist()}
    assert _cw.prepare(mine).run()
    with zipfile.ZipFile(car) as zf:
        new = {i.filename: (i.CRC, i.compress_size) for i in zf.infolist()}
    added = [k for k in new if k not in old]
    assert len(added) == 1 and added[0].endswith(".defn")
    assert all(new[k] == v for k, v in old.items()
               if k not in ("manifest.json", "integrity.json"))


def test_a_cache_write_leaves_a_file_another_program_changed(car):
    from ovtsuite.project import cachewrite as _cw
    mine = ovt.Project.load(car)
    mine.set_cache(mine.set("ECU").id, "Stage 1", b"DEFN")
    job = _cw.prepare(mine)
    _other_program(car, lambda q: q.set_info({"vin": "X"}))
    with open(car, "rb") as fh:
        data = fh.read()
    assert not job.run() and job.outcome == "changed on disk"
    with open(car, "rb") as fh:
        assert fh.read() == data
    assert mine.changed_on_disk() == "changed"      # the save still asks
    with pytest.raises(_ds.DiskChanged):
        mine.save()


def test_a_cache_for_a_moved_branch_or_an_unsaved_project_is_not_written(car):
    from ovtsuite.project import cachewrite as _cw
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    flash = _flash(mine, "Stage 1")
    mine.commit(s.id, "Stage 1", {"flash": _edit(flash, 0x300, b"\x03")})
    mine.set_cache(s.id, "Stage 1", b"DEFN-of-the-unsaved-head")
    assert _cw.prepare(mine) is None
    assert _cw.prepare(ovt.Project("new")) is None  # never saved


def test_a_cache_write_skips_a_signed_file(car, tmp_path):
    pytest.importorskip("cryptography")
    from ovtsuite.modules import signing
    from ovtsuite.project import archive as _ar
    from ovtsuite.project import cachewrite as _cw
    key = str(tmp_path / "k.pem")
    signing.generate_key(key)
    _ar.atomic_write(car, ovt.Project.load(car).to_bytes(sign_key=key,
                                                         signer="Bear"))
    mine = ovt.Project.load(car)
    assert mine.report.signature.status == "valid"
    mine.set_cache(mine.set("ECU").id, "Stock", b"DEFN")
    job = _cw.prepare(mine)
    assert not job.run() and job.outcome == "signed"
    assert ovt.Project.load(car).report.signature.status == "valid"


def test_a_save_and_a_cache_write_on_another_thread_do_not_clash(car):
    import threading
    from ovtsuite.project import cachewrite as _cw
    mine = ovt.Project.load(car)
    s = mine.set("ECU")
    mine.set_cache(s.id, "Stock", b"DEFN")
    mine.set_info({"make": "VW"})
    job = _cw.prepare(mine)
    t = threading.Thread(target=job.run)
    with mine._io_lock:
        t.start()                           # waits for the lock
        save = mine.prepare_save()
    save.run()                              # either order: never refused
    save.finish()
    t.join()
    q = ovt.Project.load(car)
    assert q.info["make"] == "VW" and q.cache(s.id, "Stock") == b"DEFN"
    assert mine.changed_on_disk() is None
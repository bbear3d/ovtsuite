# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Saving a project quickly (ovtsuite.project.archive / project / autosave).

Synthetic, fast: a re-save copies the entries that did not change instead of
hashing and compressing them again (also the first save after loading); what
does not compress is stored; the zip stays one any reader opens; the disk
check reads the file only when its size or time changed; a save can run in
three steps (the slow one on another thread); and a crash-recovery snapshot
in progress never holds a save or an exit up for long.
"""

import io
import json
import os
import random
import threading
import time
import zipfile
import zlib

import pytest

from ovtsuite import project as ovt
from ovtsuite.project import archive as _ar
from ovtsuite.project import autosave as _as
from ovtsuite.project import disksync as _ds


def _rand(n, seed):
    return random.Random(seed).randbytes(n)


def _project():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    flash = _rand(0x10000, 1) + bytes(0x10000)
    p.add_root(s.id, {"flash": flash}, kind="read", label="Read")
    p.add_log("drive.csv", b"t,rpm\n" + b"".join(
        b"%d,%d\n" % (i, i * 7 % 5000) for i in range(20000)))
    p.add_attachment("photo.jpg", _rand(200_000, 2))       # does not compress
    return p, s, flash


class _Count:
    """Counts deflate runs (zlib.compressobj) made by the archive writer."""

    def __init__(self, monkeypatch):
        self.n = 0
        real = zlib.compressobj

        def counted(*a, **k):
            self.n += 1
            return real(*a, **k)
        monkeypatch.setattr(_ar.zlib, "compressobj", counted)


def test_the_zip_is_one_any_reader_opens(tmp_path):
    p, s, flash = _project()
    data = p.to_bytes()
    assert _ar.is_project(data)
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert zf.testzip() is None                    # every CRC checks out
    names = zf.namelist()
    assert names[:3] == ["mimetype", _ar.MANIFEST, _ar.INTEGRITY]
    info = {i.filename: i for i in zf.infolist()}
    att = next(n for n in names if n.endswith("photo.jpg"))
    log = next(n for n in names if n.endswith("drive.csv"))
    assert info[att].compress_type == zipfile.ZIP_STORED
    assert info[log].compress_type == zipfile.ZIP_DEFLATED
    q = ovt.Project.from_bytes(data)
    assert q.report.ok
    assert q.head_state(q.set("ECU").id)["flash"] == flash


def test_a_resave_copies_what_did_not_change(tmp_path, monkeypatch):
    p, s, flash = _project()
    path = str(tmp_path / "car.ovt")
    p.save(path)
    count = _Count(monkeypatch)
    p.commit(s.id, None, {"flash": flash[:10] + b"\x01" + flash[11:]},
             label="one byte")
    p.save(path)
    # the new delta and the (small) manifest/integrity only -- not the log,
    # the attachment or the root image
    assert count.n <= 3
    q = ovt.Project.load(path)
    assert q.report.ok
    assert q.head_state(s.id)["flash"][10] == 1


def test_the_first_save_after_loading_copies_too(tmp_path, monkeypatch):
    p, s, flash = _project()
    path = str(tmp_path / "car.ovt")
    p.save(path)
    q = ovt.Project.load(path)
    count = _Count(monkeypatch)
    q.add_log("second.csv", b"t\n1\n" * 10)
    q.save(path)
    assert count.n <= 3
    r = ovt.Project.load(path)
    assert r.report.ok and len(r.logs) == 2
    assert r.log_bytes(r.logs[0].id) == p.log_bytes(p.logs[0].id)


def test_a_file_written_by_zipfile_loads_and_resaves(tmp_path):
    """An .ovt from an older build (zipfile's own writer) opens, and the
    entries copied from it into the next save are still sound."""
    p, s, flash = _project()
    blobs = dict(p._blobs)
    manifest = p.manifest()
    manifest["entries"] = {k: {"sha256": _ar.sha256(v), "size": len(v)}
                           for k, v in sorted(blobs.items())}
    raw = json.dumps(manifest, indent=1).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        mt = zipfile.ZipInfo("mimetype", date_time=(2026, 1, 1, 0, 0, 0))
        zf.writestr(mt, _ar.MIMETYPE)
        zf.writestr(_ar.MANIFEST, raw, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr(_ar.INTEGRITY, b'{"manifest_sha256": "%s"}'
                    % _ar.sha256(raw).encode(),
                    compress_type=zipfile.ZIP_DEFLATED)
        for k in sorted(blobs):
            zf.writestr(k, blobs[k], compress_type=zipfile.ZIP_DEFLATED)
    path = str(tmp_path / "old.ovt")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    q = ovt.Project.load(path)
    assert q.report.ok and q._zcache
    q.name = "renamed"
    q._touch()
    q.save(path)
    r = ovt.Project.load(path)
    assert r.report.ok and r.name == "renamed"
    assert zipfile.ZipFile(path).testzip() is None
    assert r.head_state(r.set("ECU").id)["flash"] == flash


def test_a_damaged_entry_is_not_copied_as_sound(tmp_path):
    p, s, flash = _project()
    data = bytearray(p.to_bytes())
    log = next(k for k in p._blobs if k.endswith("drive.csv"))
    q = ovt.Project.from_bytes(bytes(data))
    assert log in q._zcache
    # flip a byte inside the stored log: its CRC no longer matches
    zi = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo(log)
    n, x = int.from_bytes(data[zi.header_offset + 26:zi.header_offset + 28],
                          "little"), \
        int.from_bytes(data[zi.header_offset + 28:zi.header_offset + 30],
                       "little")
    data[zi.header_offset + 30 + n + x + 5] ^= 0xFF
    q = ovt.Project.from_bytes(bytes(data))
    assert log in q.report.damaged and log not in q._zcache


def test_the_disk_check_reads_the_file_only_when_it_looks_changed(
        tmp_path, monkeypatch):
    p, s, flash = _project()
    path = str(tmp_path / "car.ovt")
    p.save(path)
    reads = []
    real = _ds._file_sha
    monkeypatch.setattr(_ds, "_file_sha", lambda f: reads.append(f) or real(f))
    assert p.changed_on_disk() is None and reads == []
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert p.changed_on_disk() is None and len(reads) == 1     # touched only
    assert p.changed_on_disk() is None and len(reads) == 1     # remembered
    q = ovt.Project.load(path)
    q.name = "other"
    q._touch()
    q.save(path)
    assert p.changed_on_disk() == "changed"


def test_a_save_in_three_steps(tmp_path):
    p, s, flash = _project()
    path = str(tmp_path / "car.ovt")
    p.save(path)
    p.commit(s.id, None, {"flash": bytes(len(flash))}, label="zeros")
    job = p.prepare_save()
    t = threading.Thread(target=job.run)
    t.start()
    t.join()
    assert job.finish() == path
    assert not p.modified_since_save
    assert p.changed_on_disk() is None
    assert ovt.Project.load(path).head_state(s.id)["flash"] == bytes(len(flash))
    # another program changed it: the worker step refuses, nothing written
    q = ovt.Project.load(path)
    q.name = "theirs"
    q._touch()
    q.save(path)
    p.name = "mine"
    p._touch()
    job = p.prepare_save()
    with pytest.raises(_ds.DiskChanged):
        job.run()
    with pytest.raises(RuntimeError):
        job.finish()
    assert ovt.Project.load(path).name == "theirs"
    # a change made while it was being written keeps the project unsaved
    job = p.prepare_save(force=True)
    job.run()
    p.name = "later"
    p._touch()
    job.finish()
    assert p.modified_since_save


def _crash(saver):
    saver._lock.release()
    saver._lock = None


def test_a_slow_snapshot_does_not_hold_a_save_up(tmp_path, monkeypatch):
    root = str(tmp_path / "rec")
    p, s, flash = _project()
    p.save(str(tmp_path / "car.ovt"))
    saver = _as.AutoSaver(p, root)
    gate, entered = threading.Event(), threading.Event()
    real = _as._d.diff

    def slow(a, b):
        entered.set()
        gate.wait(10)
        return real(a, b)
    monkeypatch.setattr(_as._d, "diff", slow)
    saver.snapshot({(s.id, p.set(s.id).active.id): {"flash": bytes(len(flash))}})
    assert entered.wait(5)
    t0 = time.monotonic()
    saver.saved(wait=0.1)                 # the project was just saved
    assert time.monotonic() - t0 < 2
    gate.set()                            # the snapshot finishes...
    assert saver.flush(5)
    assert not saver.active               # ...and removes its entry itself
    assert _as.pending(root) == []


def test_a_newer_snapshot_supersedes_one_in_progress(tmp_path, monkeypatch):
    root = str(tmp_path / "rec")
    p, s, flash = _project()
    p.save(str(tmp_path / "car.ovt"))
    bid = p.set(s.id).active.id
    saver = _as.AutoSaver(p, root)
    gate, entered = threading.Event(), threading.Event()
    real = _as._d.diff
    calls = []

    def slow(a, b):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            gate.wait(10)
        return real(a, b)
    monkeypatch.setattr(_as._d, "diff", slow)
    first = flash[:4] + b"\x01" + flash[5:]
    second = flash[:4] + b"\x02" + flash[5:]
    saver.snapshot({(s.id, bid): {"flash": first}})
    assert entered.wait(5)
    t0 = time.monotonic()
    saver.snapshot({(s.id, bid): {"flash": second}})    # does not wait
    assert time.monotonic() - t0 < 1
    gate.set()
    assert saver.flush(5)
    _crash(saver)
    proj, working, _w = _as.recover(_as.pending(root)[0])
    assert working[(s.id, bid)]["flash"][4] == 2         # the newer one


def test_an_entry_discarded_mid_write_is_never_offered(tmp_path):
    root = str(tmp_path / "rec")
    p, s, flash = _project()
    p.save(str(tmp_path / "car.ovt"))
    saver = _as.AutoSaver(p, root)
    saver.snapshot({(s.id, p.set(s.id).active.id):
                    {"flash": bytes(len(flash))}}, wait=True)
    with open(os.path.join(saver.dir, _as.DISCARDED), "wb"):
        pass                              # what a timed-out discard leaves
    _crash(saver)
    assert _as.pending(root) == []
    assert not os.path.exists(saver.dir)


def test_a_scratch_container_write_is_uncompressed(tmp_path):
    from ovtsuite import changelog as _cl
    from ovtsuite import container as _c
    c = _c.Container(_c.FORMATS["edc16"])
    c.add(_c.BLOCK_EXTERNAL, bytes(0x20000))
    path = str(tmp_path / "work.edc16")
    c.save(path)
    _cl.rewrite(path, "history\n", durable=False)
    with open(path, "rb") as fh:
        raw = fh.read()
    assert len(raw) > 0x20000                     # stored, not deflated
    got = _c.Container.load(path)
    assert bytes(got.get(_c.BLOCK_EXTERNAL).data) == bytes(0x20000)
    assert "history" in (_cl.read_text(path) or "")

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An EDC16 calibration-only file (512 KB) is the 2 MB image it is a window
of: padded on import (a file or a read), on export of what an older project
holds, and merged into the original's image when it becomes a new original.
A 1 MB file (an EDC16U1's whole flash) and anything unrecognised are never
padded. Synthetic images: the calibration descriptor and marker in zeros."""
import pytest

from ovtsuite.platforms import edc16 as E
from ovtsuite import project as ovt
from ovtsuite.project import exporters as ex
from ovtsuite.project import importers as imp

CAL = E.CAL_START


def _cal(fill=0x00, tag=0x01):
    """A 512 KB calibration window: descriptor, marker, a byte to tell files apart."""
    b = bytearray([fill]) * E.CAL_SIZE
    b[0x3D:0x41] = E.CAL_MARKER
    b[0x1000:0x1004] = E.DESCRIPTOR
    b[0x2000] = tag
    return bytes(b)


def _full(tag=0x01, low=0xA5):
    return bytes([low]) * CAL + _cal(tag=tag)


def test_only_a_512_kb_edc16_window_is_calibration_only():
    assert E.is_cal_only(_cal())
    assert not E.is_cal_only(_cal()[:-1])                     # another size
    assert not E.is_cal_only(b"\x00" * E.CAL_SIZE)            # not EDC16
    assert not E.is_cal_only(bytes([0xFF]) * 0x80000 + _cal())  # 1.5 MB
    one_mb = bytes([0xFF]) * 0x80000 + _cal()
    assert len(one_mb) == 0x100000 and not E.is_cal_only(one_mb)
    padded = E.pad_cal_only(_cal())
    assert len(padded) == E.FULL_SIZE
    assert padded[:CAL] == b"\xFF" * CAL and padded[CAL:] == _cal()


def test_a_512_kb_file_imports_as_the_full_image():
    p = ovt.Project("x")
    s = imp.import_bytes(p, _cal(), "cal.bin")
    root = s.roots()[0]
    got = p.state(s.id, root.id)["flash"]
    assert len(got) == E.FULL_SIZE and got[CAL:] == _cal()
    assert got[:CAL] == b"\xFF" * CAL
    assert root.source["padded_from"] == E.CAL_SIZE
    assert s.platform == "edc16"


def test_a_change_log_beside_the_file_makes_no_difference():
    """The sidecar log is deprecated: the file is padded like any other."""
    p = ovt.Project("x")
    s = imp.import_bytes(p, _cal(), "cal.bin", sidecar_log="# notes\n")
    assert len(p.state(s.id, s.roots()[0].id)["flash"]) == E.FULL_SIZE


def test_a_1_mb_file_is_not_padded():
    one = bytes([0xFF]) * 0x80000 + _cal()
    p = ovt.Project("x")
    s = imp.import_bytes(p, one, "u1.bin")
    assert p.state(s.id, s.roots()[0].id)["flash"] == one


def test_a_512_kb_read_is_padded_too():
    p = ovt.Project("x")
    s = imp.import_read(p, _cal(), "read", source={"tool": "kline"})
    root = s.roots()[0]
    assert len(p.state(s.id, root.id)["flash"]) == E.FULL_SIZE
    assert root.kind == "read" and root.source["tool"] == "kline"


def test_an_older_projects_512_kb_member_exports_as_2_mb():
    p = ovt.Project("old")
    s = p.add_set("ECU", platform="edc16")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    p.add_root(s.id, {"flash": _cal()}, kind="import")        # as an old import held it
    out = ex.export_raw(p, s.id, "flash")
    assert len(out) == E.FULL_SIZE and out[CAL:] == _cal() and out[:CAL] == b"\xFF" * CAL
    # a full image, or another platform's 512 KB, comes out as it is
    q = ovt.Project("new")
    t = q.add_set("ECU", platform="edc16")
    q.add_member(t.id, ovt.Member(id="flash", role="flash"))
    q.add_root(t.id, {"flash": _full()}, kind="import")
    assert ex.export_raw(q, t.id, "flash") == _full()


def test_a_new_original_takes_the_calibration_and_keeps_the_rest():
    p = ovt.Project("x")
    s = imp.import_bytes(p, _full(tag=0x01, low=0xA5), "full.bin")
    old = s.roots()[0]
    new_cal = _cal(tag=0x02)                                   # the stock file
    images, source, _orig = imp.original_images(p, s.id, new_cal, "stock.bin")
    mid = next(iter(old.sizes))
    img = images[mid]
    assert len(img) == E.FULL_SIZE
    assert img[:CAL] == bytes([0xA5]) * CAL              # the old original's, not 0xFF
    assert img[CAL:] == new_cal
    assert source["merged"] and source["file"] == "stock.bin"
    assert p.original_problem(s.id, images) == ""
    c = p.rebase_original(s.id, images, source=source)
    assert p.state(s.id, c.id)[mid] == img
    assert p.state(s.id, old.id)[mid] == _full(tag=0x01, low=0xA5)   # still what it was
    assert [x.id for x in s.roots()] == [c.id]


def test_a_calibration_only_original_takes_a_calibration_only_file():
    """An older project whose original is still the 512 KB file: the same
    size in, so the file's window replaces it."""
    p = ovt.Project("old")
    s = p.add_set("ECU", platform="edc16")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    p.add_root(s.id, {"flash": _cal(tag=0x01)}, kind="import")
    images, source, _o = imp.original_images(p, s.id, _cal(tag=0x02), "stock.bin")
    assert images["flash"] == _cal(tag=0x02)
    assert p.original_problem(s.id, images) == ""


def _old_project():
    """An older project: a calibration-only original (512 KB) with a save on
    it and a branch off that."""
    p = ovt.Project("old")
    s = p.add_set("ECU", platform="edc16")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    root, stock = p.add_root(s.id, {"flash": _cal(tag=0x01)}, kind="import")
    tuned = bytearray(_cal(tag=0x01))
    tuned[0x4000:0x4004] = b"\x12\x34\x56\x78"
    c1 = p.commit(s.id, stock.id, {"flash": bytes(tuned)}, label="Stage 1")
    br = p.fork(s.id, "Experiment", at=c1.id)
    exp = bytearray(tuned)
    exp[0x5000] = 0x99
    c2 = p.commit(s.id, br.id, {"flash": bytes(exp)}, label="Try")
    p.mark_flashed(s.id, stock.id, c1.id, tool="test")
    return p, s, root, c1, c2, bytes(tuned), bytes(exp)


def test_a_full_image_replaces_a_calibration_only_original():
    """The 512 KB original and every version after it move into the
    calibration window of the 2 MB image, which becomes the original."""
    p, s, root, c1, c2, tuned, exp = _old_project()
    full = _full(tag=0x02, low=0xA5)
    images, source, _o = imp.original_images(p, s.id, full, "full.bin")
    assert images["flash"] == full and source["embed_previous"] == {"flash": CAL}
    embed = source["embed_previous"]
    assert p.original_problem(s.id, images, embed=embed) == ""
    assert "bytes" in p.original_problem(s.id, images)       # not without it
    new = p.rebase_original(s.id, images, source=source, embed=embed)
    low = bytes([0xA5]) * CAL

    def window(cid):
        st = p.state(s.id, cid)["flash"]
        assert len(st) == E.FULL_SIZE and st[:CAL] == low   # the new original's
        return st[CAL:]
    assert p.state(s.id, new.id)["flash"] == full
    assert window(root.id) == _cal(tag=0x01)                # what it was, in place
    assert window(c1.id) == tuned and window(c2.id) == exp
    assert [x.id for x in s.roots()] == [new.id]
    assert s.checkpoint(root.id).parent == new.id
    assert all(c.sizes["flash"] == E.FULL_SIZE for c in s.checkpoints)
    assert p.last_flashed(s.id)["checkpoint"] == c1.id
    # and the same after a save and a reload
    q = ovt.Project.from_bytes(p.to_bytes())
    t = q.set(s.id)
    assert q.state(t.id, c2.id)["flash"][CAL:] == exp
    assert q.state(t.id, root.id)["flash"][:CAL] == low
    assert not q.report.damaged and not q.report.missing
    # the history still goes both ways
    assert p.state(s.id, c1.id)["flash"][CAL + 0x4000:CAL + 0x4004] == b"\x12\x34\x56\x78"


def test_a_full_image_that_is_not_a_window_of_it_is_refused():
    p, s, root, c1, c2, tuned, exp = _old_project()
    other = bytes([0xA5]) * 0x100000                 # 1 MB: not what it was
    images, source, _o = imp.original_images(p, s.id, other, "other.bin")
    assert "embed_previous" not in source
    assert "bytes" in p.original_problem(s.id, images)


def test_a_history_that_changes_size_cannot_move_into_a_larger_image():
    p, s, root, c1, c2, tuned, exp = _old_project()
    p.commit(s.id, s.active.id, {"flash": _cal(tag=0x01)[:-16]}, label="short")
    full = _full(tag=0x02)
    with pytest.raises(ValueError, match="changes the size"):
        p.rebase_original(s.id, {"flash": full}, embed={"flash": CAL})
    assert [x.id for x in s.roots()] == [root.id]                  # nothing moved

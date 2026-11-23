# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Project.insert_version: a file into a binary's history at any point --
the versions after it keep their bytes."""
import random

import pytest

from ovtsuite import project as ovt


def _rand(n, seed):
    r = random.Random(seed)
    return bytes(r.getrandbits(8) for _ in range(n))


def _edit(data, off, val):
    b = bytearray(data)
    b[off:off + len(val)] = val
    return bytes(b)


@pytest.fixture
def hist():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    flash = _rand(0x4000, 1)
    root, br = p.add_root(s.id, {"flash": flash}, kind="read")
    a = p.commit(s.id, br.id, {"flash": _edit(flash, 0x10, b"\xAA")})
    b = p.commit(s.id, br.id, {"flash": _edit(
        p.state(s.id, a.id)["flash"], 0x20, b"\xBB")})
    return p, s, br, root, a, b


def test_insert_in_the_middle_keeps_later_bytes(hist, tmp_path):
    p, s, br, root, a, b = hist
    before = {c.id: p.state(s.id, c.id) for c in s.checkpoints}
    img = _edit(p.state(s.id, root.id)["flash"], 0x30, b"\xCC\xCC")
    n = p.insert_version(s.id, root.id, {"flash": img}, branch=br.id,
                         label="Imported: x.bin")
    assert n.parent == root.id and a.parent == n.id
    assert br.head == b.id                              # the head stays
    assert [c.id for c in s.lineage(br.head)] == [b.id, a.id, n.id, root.id]
    q = ovt.Project.load(p.save(str(tmp_path / "car")))
    qs = q.set(s.id)
    for cid, st in before.items():
        assert q.state(qs.id, cid) == st                # same bytes after reload
    assert q.state(qs.id, n.id)["flash"] == img
    assert qs.checkpoint(a.id).n_bytes == 3             # 0x10 back + 0x30 x2


def test_insert_after_the_head_moves_it(hist):
    p, s, br, root, a, b = hist
    img = _edit(p.state(s.id, b.id)["flash"], 0x40, b"\xDD")
    n = p.insert_version(s.id, b.id, {"flash": img}, branch=br.id)
    assert br.head == n.id and n.parent == b.id


def test_insert_leaves_another_fork_alone(hist):
    p, s, br, root, a, b = hist
    eco = p.fork(s.id, "Eco", at=root.id)
    e1 = p.commit(s.id, eco.id, {"flash": _edit(
        p.state(s.id, root.id)["flash"], 0x50, b"\xEE")})
    img = _edit(p.state(s.id, root.id)["flash"], 0x60, b"\x01")
    n = p.insert_version(s.id, root.id, {"flash": img}, branch=br.id)
    assert a.parent == n.id and e1.parent == root.id


def test_same_bytes_refused(hist):
    p, s, br, root, a, b = hist
    with pytest.raises(ValueError):
        p.insert_version(s.id, a.id, {"flash": p.state(s.id, a.id)["flash"]})

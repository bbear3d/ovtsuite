# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Naming version-history checkpoints, and merging adjacent ones.

A merge rewrites only the history (the .log beside the binary); the binary is
untouched, and reverting to a point before, inside or after the merged run
still lands on exactly the bytes it did before the merge.

Slow: each case loads a full TunerSession.
"""

import os

import pytest

from ovtsuite.session import TunerSession
from ovtsuite import history as H

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
PRIMARY = "038906016AB.bin"


def _session():
    path = os.path.join(BIN, PRIMARY)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % PRIMARY)
    return TunerSession(path)


def _bump(s, e, by=1):
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + by)])


def _saves(s, out, n):
    """n saves, each changing a different parameter; returns the bytes after
    each save, oldest first."""
    states = []
    for i in range(n):
        _bump(s, s.entities[i])
        s.save(out)
        states.append(bytes(s.binfile.data))
    return states


def test_a_saved_checkpoint_can_be_named_and_the_name_is_kept(tmp_path):
    s = _session()
    out = str(tmp_path / "named.bin")
    _saves(s, out, 2)
    newest = H.list_versions(s)[0]
    H.set_label(s, newest["id"], "  boost +10%  ")
    assert H.display_label(H.list_versions(s)[0]) == "boost +10%"
    again = TunerSession(out)                       # read back from the log
    assert H.display_label(H.list_versions(again)[0]) == "boost +10%"


def test_clearing_a_name_shows_the_automatic_label_again(tmp_path):
    s = _session()
    out = str(tmp_path / "cleared.bin")
    _saves(s, out, 1)
    vid = H.list_versions(s)[0]["id"]
    H.set_label(s, vid, "first")
    H.set_label(s, vid, "")
    assert H.display_label(H.list_versions(s)[0]) == "save"


def test_a_pending_checkpoint_is_named_and_the_name_is_saved(tmp_path):
    s = _session()
    out = str(tmp_path / "pending.bin")
    _saves(s, out, 2)
    oldest = H.list_versions(s)[-1]["id"]
    H.revert_to_version(s, oldest)                  # logs a pending checkpoint
    pending = H.list_versions(s)[0]
    assert pending["pending"]
    H.set_label(s, pending["id"], "back to stage 1")
    s.save(out)
    kept = {v["id"]: H.display_label(v)
            for v in H.list_versions(TunerSession(out))}
    assert kept[pending["id"]] == "back to stage 1"


def test_naming_leaves_every_other_log_line_alone(tmp_path):
    s = _session()
    out = str(tmp_path / "lines.bin")
    _saves(s, out, 3)
    log = os.path.splitext(out)[0] + ".log"
    before = open(log, encoding="utf-8").read().split("\n")
    H.set_label(s, H.list_versions(s)[1]["id"], "middle")
    after = open(log, encoding="utf-8").read().split("\n")
    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(changed) == 1 and '"name":"middle"' in after[changed[0]]


def test_merging_adjacent_saves_leaves_one_checkpoint_with_the_net_change(
        tmp_path):
    s = _session()
    out = str(tmp_path / "merge.bin")
    states = _saves(s, out, 4)
    live = bytes(s.binfile.data)
    v = H.list_versions(s)                          # newest first: 4,3,2,1
    H.set_label(s, v[1]["id"], "timing")
    H.set_label(s, v[2]["id"], "boost")
    rec = H.squash_versions(s, [v[1]["id"], v[2]["id"]])
    after = H.list_versions(s)
    assert [x["id"] for x in after] == [v[0]["id"], v[1]["id"], v[3]["id"]]
    assert rec["merged_from"] == [v[2]["id"], v[1]["id"]]
    assert H.display_label(after[1]) == "boost; timing"
    assert bytes(s.binfile.data) == live, "the binary itself is untouched"
    # the history still reconstructs every surviving point exactly
    assert H.bytes_at_version(s, v[0]["id"]) == states[3]
    assert H.bytes_at_version(s, v[1]["id"]) == states[2]
    assert H.bytes_at_version(s, v[3]["id"]) == states[0]


def test_reverting_after_a_merge_restores_the_same_bytes(tmp_path):
    s = _session()
    out = str(tmp_path / "revert.bin")
    states = _saves(s, out, 4)
    v = H.list_versions(s)
    H.squash_versions(s, [v[0]["id"], v[1]["id"], v[2]["id"]])
    H.revert_to_version(s, v[3]["id"])              # before the merged run
    assert bytes(s.binfile.data) == states[0]


def test_a_change_undone_inside_the_run_drops_out(tmp_path):
    s = _session()
    out = str(tmp_path / "undone.bin")
    e = s.entities[0]
    _bump(s, e, +1)
    s.save(out)
    _bump(s, e, -1)
    s.save(out)
    v = H.list_versions(s)
    rec = H.squash_versions(s, [v[0]["id"], v[1]["id"]])
    # (the first save also corrected the checksums -- that part is real)
    cell = e.addr + e.index(0, 0) * e.step
    for start, old, _new in rec["ranges"]:
        assert not start <= cell < start + len(old) // 2, \
            "an edit undone within the run was kept"


def test_merging_again_keeps_the_whole_lineage(tmp_path):
    s = _session()
    out = str(tmp_path / "again.bin")
    _saves(s, out, 3)
    v = H.list_versions(s)
    H.squash_versions(s, [v[1]["id"], v[2]["id"]])
    w = H.list_versions(s)
    rec = H.squash_versions(s, [w[0]["id"], w[1]["id"]], name="all three")
    assert rec["merged_from"] == [v[2]["id"], v[1]["id"], v[0]["id"]]
    assert rec["label"] == "merged 3 saves" and rec["name"] == "all three"


@pytest.mark.parametrize("pick, why", [
    ([0], "at least two"),
    ([0, 2], "next to each other"),
])
def test_merges_that_cannot_be_are_refused(tmp_path, pick, why):
    s = _session()
    out = str(tmp_path / "refuse.bin")
    _saves(s, out, 3)
    v = H.list_versions(s)
    with pytest.raises(ValueError, match=why):
        H.squash_versions(s, [v[i]["id"] for i in pick])


def test_an_unsaved_checkpoint_cannot_be_merged(tmp_path):
    s = _session()
    out = str(tmp_path / "unsaved.bin")
    _saves(s, out, 2)
    H.revert_to_version(s, H.list_versions(s)[-1]["id"])
    v = H.list_versions(s)
    assert v[0]["pending"]
    with pytest.raises(ValueError, match="Save first"):
        H.squash_versions(s, [v[0]["id"], v[1]["id"]])

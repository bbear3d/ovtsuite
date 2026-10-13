# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Building a patch from a single binary's own version history, instead of
a separately loaded comparison file (ovtsuite.history + ovtsuite.patch).

Slow: each case loads a full TunerSession.
"""

import os

import pytest

from ovtsuite.session import TunerSession
from ovtsuite import history as H
from ovtsuite import patch as P

pytestmark = pytest.mark.slow

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016AB.bin")),
    reason="reference files not present")

PRIMARY = "038906016AB.bin"


def _session():
    path = os.path.join(BIN, PRIMARY)
    if not os.path.exists(path):
        pytest.skip("reference binary %s not present" % PRIMARY)
    return TunerSession(path)


@needs_ref
def test_bytes_at_version_reconstructs_the_exact_pre_edit_state(tmp_path):
    s = _session()
    out = str(tmp_path / "bav1.bin")
    e0, e1 = s.entities[0], s.entities[1]

    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    snapshot_v1 = bytes(s.binfile.data)
    v1_id = H.list_versions(s)[0]["id"]

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    assert bytes(s.binfile.data) != snapshot_v1

    reconstructed = H.bytes_at_version(s, v1_id)
    assert reconstructed == snapshot_v1
    # a pure lookup -- the live session is left exactly as it was
    assert bytes(s.binfile.data) != snapshot_v1


@needs_ref
def test_bytes_at_version_does_not_mutate_or_log_anything(tmp_path):
    s = _session()
    out = str(tmp_path / "bav2.bin")
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]
    before = bytes(s.binfile.data)
    n_versions_before = len(H.list_versions(s))

    H.bytes_at_version(s, v1_id)

    assert bytes(s.binfile.data) == before
    assert len(H.list_versions(s)) == n_versions_before


@needs_ref
def test_diff_from_history_against_current_finds_the_edited_entity(tmp_path):
    s = _session()
    out = str(tmp_path / "dfh1.bin")
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]

    e1 = s.entities[1]
    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    # unsaved -- diff_from_history's "current" should pick this up too

    pt = P.diff_from_history(s, v1_id)   # newer_id=None -> current, incl. unsaved edit
    names = {e.name for e in pt.entries}
    assert e1.name in names
    assert "current" in pt.comparison_name


@needs_ref
def test_diff_from_history_between_two_saved_checkpoints(tmp_path):
    s = _session()
    out = str(tmp_path / "dfh2.bin")
    e0, e1 = s.entities[0], s.entities[1]

    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    v2_id = H.list_versions(s)[0]["id"]

    pt = P.diff_from_history(s, v1_id, v2_id)
    names = {e.name for e in pt.entries}
    # only what changed BETWEEN v1 and v2 -- e1's edit, not e0's (already
    # present in both endpoints)
    assert e1.name in names
    assert e0.name not in names


@needs_ref
def test_patch_from_history_round_trips_through_json_and_applies(tmp_path):
    s = _session()
    out = str(tmp_path / "dfh3.bin")
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]

    e1 = s.entities[1]
    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)

    pt = P.diff_from_history(s, v1_id)
    for e in pt.entries:
        e.selected = True
    path = str(tmp_path / "history.patch")
    pt.save(path)

    loaded = P.Patch.load(path)
    assert loaded.source_name == pt.source_name
    assert loaded.comparison_name == pt.comparison_name

    target = _session()
    res = P.apply_patch(loaded, target, fix_checksum=False)
    assert e1.name in res.applied


@needs_ref
def test_sessions_from_history_current_reuses_the_live_session(tmp_path):
    s = _session()
    out = str(tmp_path / "dfh4.bin")
    e0, e1 = s.entities[0], s.entities[1]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]
    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])   # unsaved, on top of v1

    older, newer = P.sessions_from_history(s, v1_id)
    assert newer is s
    assert older is not s
    # "current" (newer) carries the unsaved e1 edit; the reconstructed v1
    # (older) must not -- it has to be immune to not-yet-saved live edits.
    assert bytes(older.binfile.data) != bytes(s.binfile.data)


@needs_ref
def test_diff_from_history_raises_on_unknown_version_id(tmp_path):
    s = _session()
    with pytest.raises(ValueError):
        P.diff_from_history(s, "not-a-real-id")

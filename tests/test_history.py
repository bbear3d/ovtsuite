# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Automatic version history: every TunerSession.save() logs a checkpoint
of what changed, and an earlier one can be restored from the change log
alone (ovtsuite.history).

Slow: each case loads a full TunerSession.
"""

import os

import pytest

from ovtsuite.session import TunerSession
from ovtsuite import history as H

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
def test_save_with_no_changes_logs_nothing(tmp_path):
    s = _session()
    out = str(tmp_path / "unchanged.bin")
    # fix_checksum=False: a fresh load's checksum/digest may not already
    # match what correct_checksums() would write, which is a real, expected
    # byte change on its own and not what this test is checking -- see
    # test_second_save_with_no_edits_logs_nothing for the checksum-inclusive
    # case.
    s.save(out, fix_checksum=False)
    assert H.list_versions(s) == []


@needs_ref
def test_second_save_with_no_edits_logs_nothing(tmp_path):
    """The FIRST save of a freshly loaded file can legitimately log a
    checkpoint on its own -- correct_checksums() may fix a digest that
    was not already exactly what this build computes, which is a real byte
    change. A SECOND save with no further edits should log nothing: the
    first save already normalized the checksum, so there is nothing left
    to change."""
    s = _session()
    out = str(tmp_path / "twice.bin")
    s.save(out)
    n_after_first = len(H.list_versions(s))
    s.save(out)
    assert len(H.list_versions(s)) == n_after_first


@needs_ref
def test_each_save_logs_one_checkpoint_covering_just_that_saves_changes(tmp_path):
    s = _session()
    out = str(tmp_path / "versioned.bin")
    e0, e1 = s.entities[0], s.entities[1]

    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1 = H.list_versions(s)
    assert len(v1) == 1
    assert v1[0]["pending"] is False
    assert v1[0]["n_bytes"] > 0

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    v2 = H.list_versions(s)
    assert len(v2) == 2
    assert v2[0]["id"] != v1[0]["id"]
    assert v2[1]["id"] == v1[0]["id"]       # the earlier one is still there, older


@needs_ref
def test_revert_to_version_restores_exact_pre_edit_bytes(tmp_path):
    s = _session()
    out = str(tmp_path / "revert1.bin")
    e0, e1 = s.entities[0], s.entities[1]

    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    snapshot_v1 = bytes(s.binfile.data)
    v1_id = H.list_versions(s)[0]["id"]

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    assert bytes(s.binfile.data) != snapshot_v1

    rv = H.revert_to_version(s, v1_id)
    assert rv.n_versions == 1
    assert rv.warnings == []
    assert bytes(s.binfile.data) == snapshot_v1


@needs_ref
def test_revert_is_itself_logged_and_reversible(tmp_path):
    s = _session()
    out = str(tmp_path / "revert2.bin")
    e0, e1 = s.entities[0], s.entities[1]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]
    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    after_v2 = bytes(s.binfile.data)

    H.revert_to_version(s, v1_id)
    versions = H.list_versions(s)
    assert versions[0]["label"].startswith("revert to version")
    assert versions[0]["pending"] is True

    # the original v2 checkpoint is still in the log -- reverting straight
    # back to it re-applies what the first revert undid
    v2_id = [v["id"] for v in versions if v["label"] == "save"][0]
    H.revert_to_version(s, v2_id)
    assert bytes(s.binfile.data) == after_v2


@needs_ref
def test_revert_survives_a_real_save_and_reopen(tmp_path):
    s = _session()
    out = str(tmp_path / "persist.bin")
    e0, e1 = s.entities[0], s.entities[1]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    snapshot_v1 = bytes(s.binfile.data)
    v1_id = H.list_versions(s)[0]["id"]

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    assert os.path.exists(os.path.splitext(out)[0] + ".log")

    reopened = TunerSession(out)
    versions = H.list_versions(reopened)
    assert all(v["pending"] is False for v in versions)
    assert versions[0]["id"] != v1_id
    assert versions[1]["id"] == v1_id

    H.revert_to_version(reopened, v1_id)
    assert bytes(reopened.binfile.data) == snapshot_v1
    reopened.save(out)

    reopened2 = TunerSession(out)
    assert bytes(reopened2.binfile.data) == snapshot_v1


@needs_ref
def test_revert_warns_on_drift_since_the_checkpoint(tmp_path):
    s = _session()
    out = str(tmp_path / "drift.bin")
    e0, e1 = s.entities[0], s.entities[1]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    v1_id = H.list_versions(s)[0]["id"]

    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])
    s.save(out)
    # a further, un-checkpointed hand edit on top of the second save
    s.edit_cells(e1, [(0, 0, e1.cell(0, 0) + 1)])

    rv = H.revert_to_version(s, v1_id)
    assert rv.warnings, "expected a drift warning for the un-checkpointed edit"


def test_revert_to_unknown_id_raises():
    class Fake:
        path = "/tmp/does-not-matter.bin"
        change_log = []
    with pytest.raises(ValueError):
        H.revert_to_version(Fake(), "nope")


@needs_ref
def test_revert_to_current_version_is_a_noop(tmp_path):
    s = _session()
    out = str(tmp_path / "noop.bin")
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out)
    latest_id = H.list_versions(s)[0]["id"]
    rv = H.revert_to_version(s, latest_id)
    assert rv.n_versions == 0
    assert rv.warnings == ["already at this version"]


@needs_ref
def test_save_as_starts_a_fresh_history_at_the_new_path(tmp_path):
    s = _session()
    out_a = str(tmp_path / "a.bin")
    e0 = s.entities[0]
    s.edit_cells(e0, [(0, 0, e0.cell(0, 0) + 1)])
    s.save(out_a)
    assert len(H.list_versions(s)) == 1

    out_b = str(tmp_path / "b.bin")
    s.save(out_b)                      # Save As -- a fresh path, fresh log
    versions_b = H.list_versions(s)
    # nothing changed bytes-wise since the last save (to out_a), so Save As
    # to a brand-new path logs nothing new either
    assert versions_b == []
    assert not os.path.exists(os.path.splitext(out_b)[0] + ".log")

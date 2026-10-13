# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Version checkpoints on a container file (.edc16) are ranges in the FLAT
image the editor works on, not in the packed/compressed file bytes.

Before this, save() diffed the container bytes, so the first save of an
.edc16 logged a whole-file delta and revert_to_version wrote container
offsets back into the flash image."""
import os
import shutil

import pytest

from ovtsuite import history as H

SRC = os.path.join(os.path.dirname(__file__), "..", "tunes", "ecu-autotrq-rev1.edc16")
pytestmark = pytest.mark.skipif(not os.path.exists(SRC), reason="reference .edc16 not present")


def test_edc16_checkpoints_are_flat_image_ranges(tmp_path):
    from ovtsuite.session import TunerSession, set_ref_dir
    set_ref_dir(os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries"))
    path = str(tmp_path / "branch.edc16")
    shutil.copy(SRC, path)
    s = TunerSession(path)
    before = bytes(s.binfile.data)
    e = next(e for e in s.entities if e.name == "LIGov_nSetpointWarm_CUR")
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 20)])
    s.save(label="idle +20", backup=False)
    after = bytes(s.binfile.data)
    (v,) = H.list_versions(s)
    assert v["label"] == "idle +20"
    # a cell edit plus checksum/signature bytes -- not a whole-file delta
    assert v["n_bytes"] < 1024
    for addr, old_hex, new_hex in v["ranges"]:
        n = len(bytes.fromhex(old_hex))
        assert before[addr:addr + n] == bytes.fromhex(old_hex)
        assert after[addr:addr + n] == bytes.fromhex(new_hex)

    s2 = TunerSession(path)                      # history survives a reload
    assert [x["label"] for x in H.list_versions(s2)] == ["idle +20"]
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + 20)])
    s.save(label="idle +40", backup=False)
    first = [x for x in H.list_versions(s) if x["label"] == "idle +20"][0]
    assert H.bytes_at_version(s, first["id"]) == after


# -- the history travels INSIDE an .edc16 (ovtsuite.changelog) ------------------

def _open_copy(tmp_path, name="branch.edc16"):
    from ovtsuite.session import TunerSession, set_ref_dir
    set_ref_dir(os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries"))
    path = str(tmp_path / name)
    shutil.copy(SRC, path)
    return path, TunerSession(path)


def _bump(s, d=10):
    e = next(e for e in s.entities if e.name == "LIGov_nSetpointWarm_CUR")
    s.edit_cells(e, [(0, 0, e.cell(0, 0) + d)])


def test_edc16_history_is_written_inside_the_file_not_beside_it(tmp_path):
    from ovtsuite import edc16_container as C
    path, s = _open_copy(tmp_path)
    _bump(s)
    s.save(label="idle +10", backup=False)
    assert not os.path.exists(os.path.splitext(path)[0] + ".log")
    c = C.Edc16Container.load(path)
    text = c.get(C.BLOCK_LOG).data.decode()
    assert "VERSION " in text and "idle +10" in text
    assert c.external() == bytes(s.binfile.data)       # the image is intact

    from ovtsuite.session import TunerSession
    again = TunerSession(path)
    assert [v["label"] for v in H.list_versions(again)] == ["idle +10"]
    _bump(again, 20)
    again.save(label="idle +30", backup=False)          # appends, keeps the first
    assert [v["label"] for v in H.list_versions(TunerSession(path))] == [
        "idle +30", "idle +10"]


def test_renaming_a_checkpoint_rewrites_only_the_history_block(tmp_path):
    from ovtsuite import edc16_container as C
    path, s = _open_copy(tmp_path)
    _bump(s)
    s.save(label="idle +10", backup=False)
    before = C.Edc16Container.load(path)
    _bump(s, 5)                                   # an unsaved edit must not leak
    vid = H.list_versions(s)[0]["id"]
    H.set_label(s, vid, "as flashed")
    after = C.Edc16Container.load(path)
    assert after.external() == before.external()
    assert after.mpc() == before.mpc()
    assert H.display_label(H.list_versions(s)[0]) == "as flashed"
    # the next save starts from the renamed history, not the copy loaded earlier
    s.save(label="idle +15", backup=False)
    labels = [H.display_label(v) for v in H.list_versions(s)]
    assert labels == ["idle +15", "as flashed"]


def test_an_older_edc16_with_a_log_beside_it_folds_it_in_on_save(tmp_path):
    from ovtsuite import edc16_container as C
    from ovtsuite.session import TunerSession
    path, s = _open_copy(tmp_path)
    _bump(s)
    s.save(label="old checkpoint", backup=False)
    # rebuild the pre-embedding layout: history in a .log, none inside
    c = C.Edc16Container.load(path)
    side = os.path.splitext(path)[0] + ".log"
    with open(side, "w", encoding="utf-8", newline="") as fh:
        fh.write(c.get(C.BLOCK_LOG).data.decode())
    c.blocks = [b for b in c.blocks if b.type != C.BLOCK_LOG]
    c.save(path)

    old = TunerSession(path)
    assert [v["label"] for v in H.list_versions(old)] == ["old checkpoint"]
    _bump(old, 3)
    old.save(label="new checkpoint", backup=False)
    assert not os.path.exists(side), "the .log is folded into the file and retired"
    assert [v["label"] for v in H.list_versions(TunerSession(path))] == [
        "new checkpoint", "old checkpoint"]


def test_a_plain_binary_keeps_its_log_beside_it(tmp_path):
    from ovtsuite.session import TunerSession
    src = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries",
                       "03G906016LQ.bin")
    if not os.path.exists(src):
        pytest.skip("reference binary missing")
    path = str(tmp_path / "plain.bin")
    shutil.copy(src, path)
    s = TunerSession(path)
    _bump(s)
    s.save(label="idle +10", backup=False)
    assert os.path.exists(os.path.splitext(path)[0] + ".log")
    assert H.list_versions(TunerSession(path))[0]["label"] == "idle +10"

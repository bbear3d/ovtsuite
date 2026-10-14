# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Naming a project version (Project.set_checkpoint_label), and the name
round-tripping through a tab's change log (sessionbridge) -- given or
cleared in Version history."""

import json

from ovtsuite import project as ovt
from ovtsuite.project import sessionbridge as _sb


def _project():
    p = ovt.Project("bench")
    s = p.add_set("ECU", platform="unknown")
    p.add_member(s.id, ovt.Member(id="flash", role="flash"))
    flash = bytes(range(256)) * 16
    _root, b = p.add_root(s.id, {"flash": flash}, kind="read", label="Read")
    edited = bytearray(flash)
    edited[10] ^= 0xFF
    c = p.commit(s.id, b.id, {"flash": bytes(edited)}, label="save")
    return p, s, b, c


def test_a_name_and_back_to_the_automatic_one():
    p, s, _b, c = _project()
    p.set_checkpoint_label(s.id, c.id, "Stage 1")
    assert c.label == "Stage 1" and p.checkpoint_renamed(c)
    p.set_checkpoint_label(s.id, c.id, "Stage 1b")
    assert c.label == "Stage 1b"
    p.set_checkpoint_label(s.id, c.id, "")
    assert c.label == "save" and not p.checkpoint_renamed(c)


def test_the_name_survives_a_save_and_load(tmp_path):
    p, s, _b, c = _project()
    p.set_checkpoint_label(s.id, c.id, "Stage 1")
    path = str(tmp_path / "p.ovt")
    p.save(path)
    q = ovt.Project.load(path)
    qc = q.set(s.id).checkpoint(c.id)
    assert qc.label == "Stage 1"
    q.set_checkpoint_label(s.id, c.id, "")
    assert qc.label == "save"


def _records(text):
    return [json.loads(l.strip()[len(_sb.VERSION):]) for l in text.splitlines()
            if l.strip().startswith(_sb.VERSION)]


def test_version_history_sees_the_name_and_can_clear_it():
    p, s, b, c = _project()
    p.set_checkpoint_label(s.id, c.id, "Stage 1")
    text = _sb.history_text(p, s.id, b.head)
    rec = _records(text)[-1]
    assert rec["name"] == "Stage 1" and rec["label"] == "save"
    # unchanged: nothing to carry back
    assert _sb.read_log(p, s.id, b.head, text)[2] == {}
    # the name cleared in Version history (the record loses its "name")
    cleared = text.replace(',"name":"Stage 1"', "")
    assert cleared != text
    renames = _sb.read_log(p, s.id, b.head, cleared)[2]
    assert renames == {c.id: ""}
    p.set_checkpoint_label(s.id, c.id, renames[c.id])
    assert c.label == "save"

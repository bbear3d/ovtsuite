# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Raw byte editing (hex editor backend) and recent-files tracking."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

from ovtsuite.session import TunerSession  # noqa: E402
from ovtsuite import recent  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


@needs_ref
def test_edit_bytes_undoable():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    a = 0x1C0000
    orig = s.binfile.data[a]
    new = orig ^ 0xFF
    cmd = s.edit_bytes(a, bytes([new]))
    assert cmd is not None
    assert s.binfile.data[a] == new
    assert s.modified
    assert s.original[a] == orig            # pristine snapshot unchanged
    s.undo()
    assert s.binfile.data[a] == orig
    # a no-op edit returns None and records nothing
    assert s.edit_bytes(a, bytes([orig])) is None


@needs_ref
def test_edit_bytes_saves_through_checksum():
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    s.edit_bytes(0x1C0100, b"\x12\x34")
    assert s.binfile.data[0x1C0100] == 0x12 and s.binfile.data[0x1C0101] == 0x34
    out = os.path.join(tempfile.mkdtemp(), "out.bin")
    s.save(out)                              # must not raise; corrects checksum
    assert os.path.getsize(out) == len(s.binfile.data)


def test_recent_files(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    recent.clear_recent()
    assert recent.load_recent() == []
    f1 = tmp_path / "a.bin"; f1.write_bytes(b"x")
    f2 = tmp_path / "b.bin"; f2.write_bytes(b"y")
    recent.add_recent(str(f1))
    recent.add_recent(str(f2))
    lst = recent.load_recent()
    assert lst[0] == os.path.abspath(str(f2))     # most recent first
    assert os.path.abspath(str(f1)) in lst
    # re-adding moves to front, no duplicate
    recent.add_recent(str(f1))
    lst = recent.load_recent()
    assert lst[0] == os.path.abspath(str(f1))
    assert len(lst) == 2
    # missing files are pruned on load
    os.remove(str(f2))
    assert os.path.abspath(str(f2)) not in recent.load_recent()

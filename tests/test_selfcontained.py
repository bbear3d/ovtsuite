# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Matching is self-contained on the baked KB: no reference binary at runtime."""

import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pytest  # noqa: E402

from ovtsuite import kb as kb_mod  # noqa: E402
from ovtsuite import session as sess_mod  # noqa: E402
from ovtsuite.session import TunerSession  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016Q.Bin")),
    reason="reference files not present")


def test_kb_has_baked_fingerprints():
    k = kb_mod.load("p350")
    fp = [t for t in k.tables if t.fp]
    assert len(fp) > 400                      # structured tables carry an fp
    t = fp[0]
    assert t.fp_bytes and len(t.fp_bytes) >= 8
    assert any(t.vhash for t in k.tables)     # verify hashes baked too
    assert any(c.vhash for c in k.constants)


@needs_ref
def test_open_target_without_reference_binary(tmp_path):
    # copy a *target* out so the binary under test exists, then point the
    # (legacy) reference dir at an empty folder: matching must still work.
    tgt = tmp_path / "target.bin"
    shutil.copy(os.path.join(BIN, "038906016Q.Bin"), tgt)
    sess_mod.set_ref_dir(str(tmp_path / "no_such_ref_dir"))
    try:
        s = TunerSession(str(tgt))
        assert s.family == "p350_u8a7"      # Q: U8A7 sub-family of p350
        assert len(s.entities) > 9000
        src = {}
        for e in s.entities:
            src[e.source] = src.get(e.source, 0) + 1
        assert src.get("anchored", 0) > 500      # fingerprints still anchor
        assert src.get("verified", 0) > 3000     # verify hashes still work
        assert not hasattr(s, "refdata")         # no reference bytes loaded
    finally:
        sess_mod.set_ref_dir(BIN)


def test_kb_has_baked_a2l_names():
    k = kb_mod.load("p350")
    assert len(k.a2l_names) > 5000            # A2L name map baked in
    assert all(isinstance(a, int) for a in k.a2l_names)


@needs_ref
def test_variant_names_without_a2l_file(monkeypatch):
    """Variant/secondary tables are named from the baked KB map, so the A2L
    file is not needed at runtime."""
    from ovtsuite import variant as V
    # force the file-based A2L lookup to return nothing -> baked map only
    monkeypatch.setattr(V, "_A2L", {})
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"))
    vts = V.find_variant_tables(s)
    assert len(vts) > 100
    named = sum(1 for v in vts if not v.name.startswith("@0x"))
    assert named == len(vts)                  # every one still named

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pointer-table layout alignment: exact block placement on calibration reads."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import layout  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")


def _b(name):
    return open(os.path.join(BIN, name), "rb").read()


needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "038906016AB.bin")),
    reason="reference files not present")


# ---- synthetic -----------------------------------------------------------
def _primaries(lengths, base=0x1C1000):
    out, a = [], base
    for n in lengths:
        out.append(a)
        a += n
    return out


def test_align_identical_and_holes():
    lens = [10, 2, 66, 134, 4, 4, 2, 14, 2, 12, 2, 20, 4, 28, 6, 130, 8]
    ref = _primaries(lens)
    al = layout.align(ref, ref)
    assert al.identical and len(al.blocks) == len(ref) - 1
    # a coding-redirected entry (unknown primary) inside an identical table is
    # recovered from the reference spacing
    holed = list(ref)
    holed[5] = None
    holed[9] = None
    al = layout.align(ref, holed)
    assert al.identical
    assert al.tgt_redirected == 2


def test_align_shift_and_change():
    lens = [10, 2, 66, 134, 4, 4, 2, 14, 2, 12, 2, 20, 4, 28, 6, 130, 8, 40, 12, 6]
    ref = _primaries(lens)
    # target: block 8 grew by 6 bytes -> everything after shifts by +6
    tl = list(lens)
    tl[8] += 6
    tgt = _primaries(tl)
    al = layout.align(ref, tgt)
    assert not al.identical
    deltas = {b.index: b.delta for b in al.blocks}
    assert all(d == 0 for i, d in deltas.items() if i < 8)
    assert all(d == 6 for i, d in deltas.items() if i > 8)
    assert 8 not in deltas                      # the changed block is not exact
    b = al.block_at(ref[10] + 3)
    assert b is not None and b.delta == 6
    assert al.block_at(ref[8]) is None
    assert al.block_at(ref[7], span=lens[7] + 1) is None   # spills past its block


def test_short_runs_do_not_pair():
    ref = _primaries([4, 4, 100, 4, 4, 200, 4, 4])
    tgt = _primaries([4, 4, 300, 4, 4])
    al = layout.align(ref, tgt)
    # the 4,4 pairs are too common to pair on their own (MIN_RUN)
    assert al.paired == 0 and not al.blocks


# ---- reference binaries ------------------------------------------------
@needs_ref
def test_descriptor_and_table_p350():
    d = _b("038906016AB.bin")
    desc = layout.find_descriptor(d)
    assert desc is not None and desc.count == 372
    assert desc.active == 0x180084 and desc.default_lo == 0x4B998
    pt = layout.pointer_table(d)
    assert pt.from_default and pt.count == 372 and pt.redirected == 35
    assert pt.primaries[0] == 0x1C14D4


@needs_ref
def test_blank_active_falls_back_to_default():
    # A dataset with no coding overlay leaves the active (coding-selected) array
    # blank (0xFFFFFFFF) and the ECU uses the default array directly -- as on the
    # 03G906016AC/P447 BRM image, where the old code returned no table at all and
    # forced every parameter to projection.  pointer_table must fall back.
    d = bytearray(_b("038906016AB.bin"))
    good = layout.pointer_table(bytes(d))
    assert good is not None and good.from_default
    desc = layout.find_descriptor(bytes(d))
    for k in range(desc.count):                        # blank the whole active array
        d[desc.active + 4 * k: desc.active + 4 * k + 4] = b"\xFF\xFF\xFF\xFF"
    pt = layout.pointer_table(bytes(d))
    assert pt is not None and pt.from_default           # not None
    assert pt.primaries == good.primaries               # default array still drives placement
    assert pt.active == good.primaries                  # active mirrors default (no overlay)


@needs_ref
def test_catalog_has_references():
    from ovtsuite.match import REF_BINARIES
    for fam in REF_BINARIES:
        assert layout.reference_layout(fam) is not None, fam


@needs_ref
def test_calibration_read_places_like_full_binary():
    """The last 512 KB alone (what a K-line read returns) must place every
    parameter at the same address as the full binary, with nothing left
    'projected' when the layout is the reference's."""
    from ovtsuite.session import TunerSession
    from ovtsuite.flash import image
    full_path = os.path.join(BIN, "038997016T.Bin")
    d = _b("038997016T.Bin")
    img = image.normalize(d[-0x80000:])
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "T_read.bin")
    with open(p, "wb") as fh:
        fh.write(img)
    full = TunerSession(full_path)
    read = TunerSession(p)
    fa = {e.name: e.addr for e in full.entities}
    ra = {e.name: e.addr for e in read.entities}
    assert fa == ra
    c = read.result.counts()
    assert c.get("projected", 0) == 0
    assert read.result.layout is not None and read.result.layout.identical
    known, ident = layout.identify(img, "p350")
    assert known is not None and ident


@needs_ref
def test_shifted_layout_q_uses_pointer_blocks():
    """Q's layout differs from the reference; the pointer table still places
    most of it exactly and agrees with the fingerprint anchors.

    Forced to the p350 parent: by dataset id Q now routes to its own exact
    U8A7 sub-family (tests/test_p350_u8a7_kb.py); this exercises the
    shifted-layout path that sub-family was derived from."""
    from ovtsuite.session import TunerSession
    s = TunerSession(os.path.join(BIN, "038906016Q.Bin"), family="p350")
    r = s.result
    lay = r.layout
    assert lay is not None and not lay.identical
    assert len(lay.blocks) >= 300
    # every surviving fingerprint anchor inside an exact block agrees with it
    for a in r.anchors:
        if a.name.startswith("ptr:"):
            continue
        b = lay.block_at(a.ref_addr)
        if b is not None:
            assert a.offset == b.delta, a.name
    c = r.counts()
    assert c.get("projected", 0) < 600

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""M2 acceptance tests: anchor matching, offset model, audit, named diff."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")

def _b(name):
    return open(os.path.join(BIN, name), "rb").read()

needs_ref = pytest.mark.skipif(
    not os.path.exists(os.path.join(BIN, "03G906016LQ.bin")),
    reason="reference files not present")


@needs_ref
def test_holdout_identity_lq():
    """LQ matched against its own KB must place every item at identity."""
    from ovtsuite import kb
    from ovtsuite.match import match
    from ovtsuite.structure import find_cal_region
    k = kb.load("p475_83")
    d = _b("03G906016LQ.bin")
    r = match(k, d, find_cal_region(d))
    assert len(r.anchors) > 900
    assert all(a.offset == 0 for a in r.anchors)
    assert all(i.target_addr == i.ref_addr for i in r.items)


@needs_ref
def test_same_layout_targets_place_exact():
    """KN and T share their KB's layout: zero off-identity placements."""
    from ovtsuite import kb
    from ovtsuite.match import match, REF_BINARIES
    from ovtsuite.structure import find_cal_region
    for fam, tgt in (("p475_83", "03G906016KN.bin"),
                     ("p350", "038997016T.Bin")):
        k = kb.load(fam)
        ref = _b(REF_BINARIES[fam])
        d = _b(tgt)
        r = match(k, d, find_cal_region(d))
        errs = [i for i in r.items if i.target_addr != i.ref_addr]
        assert not errs, "%s: %d misplaced" % (tgt, len(errs))


@needs_ref
def test_shifted_layout_q():
    """The user's Q binary (shifted layout) still anchors extensively."""
    from ovtsuite import kb
    from ovtsuite.match import match, REF_BINARIES
    from ovtsuite.structure import find_cal_region
    k = kb.load("p350")
    r = match(k, _b("038906016Q.Bin"),
              find_cal_region(_b("038906016Q.Bin")))
    assert len(r.anchors) >= 650
    c = r.counts()
    assert c.get("anchored", 0) + c.get("struct", 0) >= 650


@needs_ref
def test_audit_near_perfect_xdf():
    from ovtsuite import kb
    from ovtsuite.audit import audit
    r = audit(kb.load("p475_83"), _b("03G906016LQ.bin"))
    assert r.ok / r.checked > 0.85
    missing = [f for f in r.findings if f.kind == "missing"]
    assert len(missing) <= 12
    # the known defective entry must be flagged (its byte-packed storage
    # defies the standard layouts, so it surfaces as 'missing')
    names = {f.name: f for f in r.findings}
    assert "CrCtl_facDevVDspl_CUR" in names


@needs_ref
def test_stage2_diff_attribution():
    """Acceptance: Stage2-vs-stock cal-region diffs land in named entities."""
    from ovtsuite import kb
    from ovtsuite.diff import diff
    from ovtsuite.match import match, REF_BINARIES
    from ovtsuite.structure import find_cal_region
    k = kb.load("p350")
    stock = _b("038906016Q.Bin")
    stg2 = _b("038906016Q-Stg2-MPPS Read.Bin")
    region = find_cal_region(stock)
    ident = match(k, stock, region)
    r = diff(stock, stg2, ident, (0, len(stock)))
    assert r.diff_bytes == 3660
    assert len(r.entities) >= 45
    incal = sum(e - s for s, e in r.gaps if region[0] <= s < region[1])
    assert incal <= 40   # only the pre-cal checksum block
    names = {e.name for e in r.entities}
    # the tune's signature moves must be named
    assert "EngPrt_trqLimP_MAP" in names       # torque limiter
    assert "PCR_pBDesBas_MAP" in names         # boost setpoint
    assert any(n.startswith("AccPed_trqEng") for n in names)

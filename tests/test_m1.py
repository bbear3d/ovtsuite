# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""M1 acceptance tests: parser, equation, detector, KB."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite.equation import Equation  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")
BIN = os.path.join(REF, "Binaries")
XDF = os.path.join(REF, "XDFs")
LQ = os.path.join(BIN, "03G906016LQ.bin")
LQ_XDF = os.path.join(XDF, "03G906016KN-LQ-BPW.xdf")

needs_ref = pytest.mark.skipif(not os.path.exists(LQ),
                               reason="reference files not present")


def test_equation_linear():
    e = Equation("(X)/(8192)")
    assert e.linear and abs(e.decode(8192) - 1.0) < 1e-9
    assert abs(e.encode(1.0) - 8192) < 1e-6
    e = Equation("(X)/(10)+(-273.14)")
    assert abs(e.decode(2731.4) - 0.0) < 1e-6
    e = Equation("X")
    assert e.decode(42) == 42


@needs_ref
def test_xdf_parse_counts():
    from ovtsuite.xdf import parse_xdf
    d = parse_xdf(LQ_XDF)
    assert len(d.tables) == 3121
    assert len(d.constants) == 8443
    kinds = {}
    for t in d.tables:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    assert kinds == {"indexed": 2032, "curve": 548, "map": 541}


@needs_ref
def test_cal_region_lq_and_t5():
    from ovtsuite.binary import BinaryFile
    from ovtsuite.structure import find_cal_region
    lq = find_cal_region(bytes(BinaryFile.load(LQ).data))
    assert lq[0] <= 0x1C0000 < lq[1] and lq[1] >= 0x1F4000
    t5 = os.path.join(BIN, "038906016N-T5")
    if os.path.exists(t5):
        r = find_cal_region(bytes(BinaryFile.load(t5).data))
        assert r[0] <= 0xD0000 < r[1] and r[1] >= 0xF4000


@needs_ref
def test_detector_recall_on_lq():
    from ovtsuite.binary import BinaryFile
    from ovtsuite.coverage import measure
    from ovtsuite.xdf import parse_xdf
    cov = measure(bytes(BinaryFile.load(LQ).data), parse_xdf(LQ_XDF))
    assert cov.expected > 1000
    assert cov.recall >= 0.92
    assert cov.exhaustive_exact / cov.expected >= 0.995


def test_kb_load():
    from ovtsuite import kb
    fams = kb.available()
    if not fams:
        pytest.skip("no baked KBs")
    k = kb.load(fams[0])
    assert k.tables and k.constants
    assert k.tables[0].name

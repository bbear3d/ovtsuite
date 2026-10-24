# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Patches across a resized table: a patch captured from a tune whose curve
was resized carries the table, not its header bytes, and applying it to a
stock file resizes the target table the same way -- its record ends up
byte for byte as the tune's, never a new point count over the old layout.

Run on its own (never the whole suite):  pytest tests/test_patch_resize.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import BINARIES  # noqa: E402
from ovtsuite import patch as P  # noqa: E402
from ovtsuite import resize as R  # noqa: E402
from ovtsuite.session import TunerSession  # noqa: E402

pytestmark = pytest.mark.slow          # whole-image sessions

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
CUR = "ACC_rBrk_CUR"                   # LQ: stores 5, reserves 8


def _ent(s, name):
    return next(e for e in s.entities if e.name == name)


@pytest.fixture
def pair():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    stock = TunerSession(LQ)
    tuned = TunerSession(LQ)
    e = _ent(tuned, CUR)
    R.execute(tuned, R.plan(tuned, e, R.spread(e.x_values(), 7, "extend")))
    return stock, tuned


def test_a_resized_table_is_not_captured_as_raw_bytes(pair):
    stock, tuned = pair
    e = _ent(tuned, CUR)
    codec = R.codec_for(tuned)
    start = codec.start_of(e)
    gaps = P.diff_gaps(stock, tuned)
    assert not [g for g in gaps if g[0] < e.end and g[1] > start]
    (pe,) = [x for x in P.diff_entities(stock, tuned) if x.name == CUR]
    assert (pe.rows, pe.cols) == (1, 7)


def test_applying_it_resizes_the_target_table(pair):
    stock, tuned = pair
    entries = [x for x in P.diff_entities(stock, tuned) if x.name == CUR]
    patch = P.new_patch(stock, tuned, entries)
    target = TunerSession(LQ)
    res = P.apply_patch(patch, target, default_disposition=P.APPLY,
                        fix_checksum=False)
    assert res.applied == [CUR] and not res.skipped
    assert any("resized" in w for w in res.warnings)
    te, se = _ent(target, CUR), _ent(tuned, CUR)
    assert te.cols == 7
    codec = R.codec_for(target)
    a, b = codec.start_of(te), se.end
    assert bytes(target.binfile.data[a:b]) == bytes(tuned.binfile.data[a:b])
    # one undo step takes the table back to its stock size
    target.undo()
    assert _ent(target, CUR).cols == 5
    assert bytes(target.binfile.data) == bytes(stock.binfile.data)

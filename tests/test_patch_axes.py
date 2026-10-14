# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A patch carries a map whose only change is its axis.

A map's breakpoints are stored outside its Z bytes (before the map, on EDC16
and the ZF boxes). A patch compared Z only, so an axis-only edit made no
entry -- and the changed breakpoint bytes, covered by no parameter's
``[addr, end)``, were offered as "undefined data" instead.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import BINARIES  # noqa: E402

LQ = os.path.join(BINARIES, "03G906016LQ.bin")
MAP = "AccPed_trqEng0_MAP"


@pytest.fixture(scope="module")
def pair():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    from ovtsuite.session import TunerSession
    stock = TunerSession(LQ)
    tuned = TunerSession(LQ)
    e = next(x for x in tuned.entities if x.name == MAP)
    want = e.x_values()[2] + 100
    tuned.edit_axis(e.x, 2, want)
    assert e.x_values()[2] == pytest.approx(want, abs=1)
    return stock, tuned, want


def test_an_axis_only_change_is_a_patch_entry(pair):
    from ovtsuite import patch as P
    stock, tuned, want = pair
    entries = {pe.name: pe for pe in P.diff_entities(stock, tuned)}
    assert MAP in entries, "the axis-only edit made no entry"
    pe = entries[MAP]
    assert pe.changed_cells == 0 and pe.changed_axes == 1
    assert "X axis[2]" in pe.first_change


def test_its_bytes_are_not_undefined_data(pair):
    from ovtsuite import patch as P
    stock, tuned, _want = pair
    e = next(x for x in stock.entities if x.name == MAP)
    w = e.x.bits // 8
    lo, hi = e.x.addr, e.x.addr + e.x.count * w
    for g0, g1 in P.diff_gaps(stock, tuned):
        assert g1 <= lo or g0 >= hi, "axis bytes offered as undefined data"


def test_applying_it_writes_the_axis_in_one_undo_step():
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    from ovtsuite import patch as P
    from ovtsuite.session import TunerSession
    stock, tuned = TunerSession(LQ), TunerSession(LQ)
    e = next(x for x in tuned.entities if x.name == MAP)
    tuned.edit_axis(e.x, 2, e.x_values()[2] + 100)
    pt = P.new_patch(stock, tuned, P.diff_entities(stock, tuned))
    target = TunerSession(LQ)
    te = next(x for x in target.entities if x.name == MAP)
    before_x, before_z = te.x_values(), te.values()
    res = P.apply_patch(pt, target, fix_checksum=False)
    assert MAP in res.applied
    assert te.x_values() == pytest.approx(e.x_values())
    assert te.values() == before_z
    target.undo()
    assert te.x_values() == pytest.approx(before_x)


def test_a_patch_from_before_writes_cells_only():
    """An entry with no ``changed_axes`` (a patch saved by an older version)
    applies exactly as it did: values, not axes."""
    if not os.path.exists(LQ):
        pytest.skip("reference binary missing: %s" % LQ)
    from ovtsuite import patch as P
    from ovtsuite.session import TunerSession
    stock, tuned = TunerSession(LQ), TunerSession(LQ)
    e = next(x for x in tuned.entities if x.name == MAP)
    tuned.edit_axis(e.x, 2, e.x_values()[2] + 100)
    tuned.edit_cells(e, [(0, 0, e.cell(0, 0) + 10)])
    d = P.new_patch(stock, tuned, P.diff_entities(stock, tuned)).to_dict()
    for ent in d["entries"]:
        ent.pop("changed_axes", None)
    old = P.Patch.from_dict(d)
    target = TunerSession(LQ)
    te = next(x for x in target.entities if x.name == MAP)
    before_x = te.x_values()
    P.apply_patch(old, target, fix_checksum=False)
    assert te.x_values() == pytest.approx(before_x)
    assert te.cell(0, 0) == pytest.approx(e.cell(0, 0))

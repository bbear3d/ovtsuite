# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Relative patch entries (ovtsuite.patch_relative): a change carried as
operations on the target's own values, cells picked by axis value, written
only where they change, reaching every coding-variant copy.

The first group runs on a stand-in table; the last cases load a real EDC16
image and are slow."""
import os

import pytest

from ovtsuite import patch as P
from ovtsuite import patch_relative as R


class _Table:
    kind = "map"
    name = "T_MAP"

    def __init__(self, xs, ys, vals):
        self.xs, self.ys, self.vals = xs, ys, [list(r) for r in vals]
        self.rows, self.cols = len(ys), len(xs)

    def x_values(self):
        return self.xs

    def y_values(self):
        return self.ys

    def values(self):
        return [list(r) for r in self.vals]


def _t():
    return _Table([1000, 2000, 3000], [10, 50, 60],
                  [[1, 2, 3], [4, 5, 6], [7, 8, 9]])


def test_add_reaches_only_the_cells_in_its_axis_ranges():
    cells = R.cells_for(_t(), [{"op": "add", "value": 1, "x": [2000, 3000],
                                "y": [55, None]}])
    assert cells == [(2, 1, 9), (2, 2, 10)]


def test_ops_run_in_order_on_each_others_result():
    cells = dict(((r, c), v) for r, c, v in R.cells_for(
        _t(), [{"op": "scale", "value": 2}, {"op": "cap", "value": 10}]))
    assert cells[(0, 0)] == 2 and cells[(1, 2)] == 10 and cells[(2, 2)] == 10


def test_floor_and_cap_leave_cells_already_inside_alone():
    assert R.cells_for(_t(), [{"op": "floor", "value": 0}]) == []
    assert R.cells_for(_t(), [{"op": "cap", "value": 100}]) == []
    assert R.cells_for(_t(), [{"op": "floor", "value": 8}]) == [
        (0, 0, 8), (0, 1, 8), (0, 2, 8), (1, 0, 8), (1, 1, 8), (1, 2, 8),
        (2, 0, 8)]


def test_a_curve_operand_is_interpolated_along_its_axis_and_held_flat():
    cells = dict(((r, c), v) for r, c, v in R.cells_for(
        _t(), [{"op": "set", "curve": [[1500, 10], [2500, 20]], "y": [60, 60]}]))
    assert cells == {(2, 0): 10, (2, 1): 15, (2, 2): 20}
    by_y = R.cells_for(_t(), [{"op": "set", "curve": [[10, 0], [60, 5]],
                               "along": "y", "x": [1000, 1000]}])
    # y=50 reads 4 off the curve -- what the cell already holds, so no write
    assert by_y == [(0, 0, 0), (2, 0, 5)]


def test_a_delta_grid_adds_its_differences_and_skips_its_zeros():
    a, b = _t(), _t()
    b.vals[2][1] += 1.5
    rel = R.delta_entry(a, b)
    target = _Table([1000, 2000, 3000], [10, 50, 60],
                    [[0, 0, 0], [0, 0, 0], [100, 100, 100]])
    assert R.cells_for(target, rel.ops) == [(2, 1, 101.5)]
    # other breakpoints: the difference is read at the target's own
    other = _Table([1500, 2000], [60], [[0, 0]])
    assert R.cells_for(other, rel.ops) == [(0, 0, 0.75), (0, 1, 1.5)]
    assert R.delta_entry(a, _t()) is None


def test_a_percentage_change_scales_with_the_targets_own_values():
    a, b = _t(), _t()
    b.vals[2][2] = 9 * 1.10                     # +10 %
    b.vals[0][0] = 0.5                          # was 1: -50 %
    rel = R.delta_entry(a, b, percent=True)
    assert rel.ops[0]["op"] == "scale_grid"
    target = _Table([1000, 2000, 3000], [10, 50, 60],
                    [[4, 0, 0], [0, 0, 0], [0, 0, 100]])
    got = dict(((r, c), v) for r, c, v in R.cells_for(target, rel.ops))
    assert got[(2, 2)] == pytest.approx(110) and got[(0, 0)] == pytest.approx(2)


def test_a_cell_the_source_held_at_zero_carries_its_change_as_an_addition():
    a = _Table([1000, 2000], [10], [[0, 5]])
    b = _Table([1000, 2000], [10], [[3, 5]])
    rel = R.delta_entry(a, b, percent=True)
    target = _Table([1000, 2000], [10], [[1, 5]])
    assert R.cells_for(target, rel.ops) == [(0, 0, 4)]


def test_only_coding_reaches_just_the_copies_that_coding_reads():
    auto, manual, dsg = _t(), _t(), _t()
    auto.name, auto.coding = "T_MAP", "coding 150034 (automatic (TC))"
    manual.name, manual.coding = "T_MAP (variant @0x1)", "coding 150031 (5-speed manual)"
    dsg.name, dsg.coding = "T_MAP (variant @0x2)", "coding 150038 (DSG)"

    def find(_s, name, _coding=""):
        return auto if name == "T_MAP" else None

    pe = R.relative_entry("T_MAP", [{"op": "cap", "value": 1}],
                          only_coding="automatic (TC)")
    got = R.targets_for(None, pe, find, lambda _s: [manual, dsg])
    assert got == [auto]
    pe.only_coding = ""
    assert R.targets_for(None, pe, find, lambda _s: [manual, dsg]) == [auto, manual, dsg]


def test_a_relative_entry_round_trips_and_rejects_a_bad_op():
    pe = R.relative_entry("T_MAP", [{"op": "add", "value": 1, "y": [55, None]}])
    back = P.PatchEntry.from_dict(pe.to_dict())
    assert back.relative and back.ops == pe.ops and back.variants
    assert "add +1" in back.first_change
    with pytest.raises(R.OpError):
        R.relative_entry("T_MAP", [{"op": "multiply", "value": 2}])
    with pytest.raises(R.OpError):
        R.relative_entry("T_MAP", [{"op": "add"}])


def test_an_older_patch_without_the_fields_still_loads_as_values():
    d = R.relative_entry("T_MAP", [{"op": "add", "value": 1}]).to_dict()
    for k in ("mode", "ops", "variants"):
        d.pop(k)
    assert not P.PatchEntry.from_dict(d).relative


# -- a real image ------------------------------------------------------------

BIN = os.path.join(os.path.dirname(__file__), "..", "Reference", "Binaries",
                   "038906016AB.bin")


@pytest.fixture(scope="module")
def ab():
    if not os.path.exists(BIN):
        pytest.skip("reference binary 038906016AB.bin not present")
    from ovtsuite.session import TunerSession
    return TunerSession(BIN)


def _copies(s, base):
    return [e for e in list(s.entities) + list(P._variants(s))
            if (e.name or "").split(" (variant @", 1)[0] == base]


@pytest.mark.slow
def test_applying_reaches_every_variant_copy_and_only_the_chosen_rows(ab):
    base = "InjCrv_phiBas8_GMAP"
    copies = _copies(ab, base)
    if len(copies) < 2:
        pytest.skip("this image keeps no variant copy of %s" % base)
    before = {e.name: e.values() for e in copies}
    pt = P.Patch(entries=[R.relative_entry(
        base, [{"op": "add", "value": 1.0, "x": [1750, 3500], "y": [55, None]}])])
    res = P.apply_patch(pt, ab, fix_checksum=False)
    try:
        assert res.applied == [base] and not res.skipped
        for e in copies:
            ys, xs, now = e.y_values(), e.x_values(), e.values()
            for r in range(e.rows):
                for c in range(e.cols):
                    d = now[r][c] - before[e.name][r][c]
                    if ys[r] >= 55 and 1750 <= xs[c] <= 3500:
                        assert d == pytest.approx(1.0, abs=0.05)
                    else:
                        assert d == 0
    finally:
        ab.undo()


@pytest.mark.slow
def test_no_variants_leaves_the_copies_alone(ab):
    base = "InjCrv_phiBas8_GMAP"
    copies = _copies(ab, base)
    if len(copies) < 2:
        pytest.skip("this image keeps no variant copy of %s" % base)
    before = {e.name: e.values() for e in copies}
    pe = R.relative_entry(base, [{"op": "add", "value": 1.0}], variants=False)
    P.apply_patch(P.Patch(entries=[pe]), ab, fix_checksum=False)
    try:
        changed = [e.name for e in copies if e.values() != before[e.name]]
        assert changed == [base]
    finally:
        ab.undo()

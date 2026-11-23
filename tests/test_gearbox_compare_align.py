# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A comparison schedule on another pedal axis, read at this file's pedal points
(ovtsuite.gearbox_edit.compare.align_values and Pairing.aligned): without it such a
pair is drawn on its own axis but not compared or copied -- with it, it is."""
from types import SimpleNamespace

from ovtsuite import gearbox_edit as GE
from ovtsuite.gearbox_edit.model import Line, Schedule, ScheduleSet

OFF = 10000


def test_values_are_read_at_the_other_pedal_points():
    # two lines; the comparison has points at 0, 50, 100 %, we ask for 0, 25, 75, 100
    vals = [100, 1000, 200, 2000, 400, 4000]
    out = GE.align_values([0, 50, 100], vals, 2, [0, 25, 75, 100])
    assert out == [100, 1000, 150, 1500, 300, 3000, 400, 4000]


def test_end_values_are_held_beyond_the_comparisons_range():
    out = GE.align_values([10, 90], [100, 900], 1, [0, 10, 50, 90, 100])
    assert out == [100, 100, 500, 900, 900]


def test_an_off_marker_is_never_blended_with_a_speed():
    off = lambda v: v == OFF            # noqa: E731
    out = GE.align_values([0, 100], [OFF, 1000], 1, [10, 40, 60, 90], off)
    assert out == [OFF, OFF, 1000, 1000]        # the nearer of the two, never 5500


def test_an_axis_that_cannot_be_read_along_gives_none():
    assert GE.align_values([50], [1], 1, [0, 100]) is None
    assert GE.align_values([0, 100, 50], [1, 2, 3], 1, [0]) is None
    assert GE.align_values([0, float("nan")], [1, 2], 1, [0]) is None
    assert GE.align_values([0, 100], [1, 2, 3], 1, [0]) is None        # wrong length


def _set(pedals):
    lines = [Line(0, "a", "up"), Line(1, "b", "up")]
    scheds = [Schedule(key="K%d" % n, entity=SimpleNamespace(rows=3, cols=2),
                       pedal=tuple(p)) for n, p in enumerate(pedals)]
    return ScheduleSet(scheds, lines)


def test_a_pair_on_another_axis_compares_only_when_aligned():
    a = _set([[0, 50, 100], [0, 50, 100]])
    b = _set([[0, 50, 100], [0, 40, 100]])
    p = GE.pair_sets(a, b)
    assert p.pairs == {0: 0, 1: 1} and p.axis_differs == {1}
    assert p.comparable(0) and not p.comparable(1)
    assert "drawn, not compared" in p.summary()
    p.aligned = set(p.axis_differs)
    assert p.comparable(0) and p.comparable(1)
    assert "read at this file's pedal points" in p.summary()


def test_copying_an_aligned_pair_takes_the_values_at_this_files_pedal_points():
    a = _set([[0, 50, 100]])
    b = _set([[0, 40, 100]])
    p = GE.pair_sets(a, b)
    cells = [(0, r, 0) for r in range(3)]
    mine = {0: [1, 1, 2, 2, 3, 3]}
    # not aligned: not comparable, so nothing to copy
    assert GE.copy_op(p, cells, {0: [9, 9, 9, 9, 9, 9]}, "copy", current=mine) is None
    p.aligned = set(p.axis_differs)
    seen = GE.align_values(b.schedules[0].pedal, [10, 0, 20, 0, 40, 0], 2,
                           a.schedules[0].pedal)
    op = GE.copy_op(p, cells, {0: seen}, "copy", current=mine)
    assert op is not None
    assert {(r, c): v for (_i, r, c), v in op.values.items()} == {
        (0, 0): 10, (1, 0): 20 - 2.0 / 6 * 0 + (50 - 40) / 60 * (40 - 20), (2, 0): 40}


def _lines(*labels):
    return [Line(i, lab, "tcc") for i, lab in enumerate(labels)]


def _tset(labels, pedal, ncols):
    sch = [Schedule(key="K", entity=SimpleNamespace(rows=len(pedal), cols=ncols),
                    pedal=tuple(pedal), ncols=ncols)]
    return ScheduleSet(sch, _lines(*labels), kind="tcc")


def test_sets_with_different_lines_are_paired_by_name():
    """A lock-up map of 30 columns has three characteristics per gear, one of 40 has four."""
    four = _tset(["RO", "OR", "RC", "COC"], [0, 50, 100], 4)
    three = _tset(["RO", "OR", "RC"], [0, 50, 100], 3)
    p = GE.pair_sets(four, three)
    assert p.ok and p.line_map == {0: 0, 1: 1, 2: 2}
    assert p.cmp_col(2) == 2 and p.cmp_col(3) is None
    assert "lines matched by name (COC not in the comparison)" in p.summary()
    q = GE.pair_sets(three, four)
    assert q.ok and q.cmp_col(1) == 1 and "lines matched by name" in q.summary()
    # nothing in common: no comparison
    other = _tset(["XX", "YY"], [0, 50, 100], 2)
    assert not GE.pair_sets(four, other).ok


def test_names_are_matched_whatever_their_order():
    a = _tset(["RO", "OR", "RC"], [0, 50, 100], 3)
    b = _tset(["RC", "RO", "OR", "COC"], [0, 50, 100], 4)
    assert GE.pair_sets(a, b).line_map == {0: 1, 1: 2, 2: 0}


def test_a_different_number_of_pedal_points_is_a_pedal_axis_that_differs():
    a = _tset(["RO"], [0, 50, 100], 1)
    b = _tset(["RO"], [0, 25, 50, 75, 100], 1)
    p = GE.pair_sets(a, b)
    assert p.ok and p.axis_differs == {0} and not p.comparable(0)
    assert "drawn, not compared" in p.summary()


def test_missing_columns_come_back_as_none_and_are_not_copied():
    vals = [100, 200, 300, 110, 210, 310]                  # 2 pedal points x 3 columns
    out = GE.align_values([0, 100], vals, 3, [0, 50, 100], None, [2, None, 0])
    assert out == [300, None, 100, 305, None, 105, 310, None, 110]
    a = _tset(["RO", "OR", "RC", "COC"], [0, 100], 4)
    b = _tset(["RO", "OR", "RC"], [0, 100], 3)
    p = GE.pair_sets(a, b)
    mine = {0: [1, 1, 1, 1, 2, 2, 2, 2]}
    seen = {0: [5, 5, 5, None, 6, 6, 6, None]}
    op = GE.copy_op(p, [(0, 0, 3), (0, 1, 3), (0, 0, 0)], seen, "copy", current=mine)
    assert op is not None and set(op.values) == {(0, 0, 0)}


def test_average_op_moves_cells_part_of_the_way_and_skips_what_it_cannot():
    a = _tset(["RO", "OR", "RC", "COC"], [0, 100], 4)
    b = _tset(["RO", "OR", "RC"], [0, 100], 3)
    a.schedules[0].off = OFF
    b.schedules[0].off = OFF
    p = GE.pair_sets(a, b)
    mine = {0: [100.0, 100.0, OFF, 100.0, 200.0, 200.0, 200.0, 200.0]}
    seen = {0: [200.0, 100.0, 300.0, None, 400.0, OFF, 300.0, None]}
    cells = [(0, r, c) for r in range(2) for c in range(4)]
    op = GE.average_op(p, cells, seen, "avg", 50.0, mine)
    assert op is not None
    # (0,0,0) halfway; (0,0,1) equal; (0,0,2) my "off"; (0,0,3) none there;
    # (0,1,0) halfway; (0,1,1) their "off"; (0,1,2) halfway; (0,1,3) none there
    assert op.values == {(0, 0, 0): 150.0, (0, 1, 0): 300.0, (0, 1, 2): 250.0}
    assert GE.average_op(p, cells, seen, "avg", 0.0, mine) is None
    full = GE.average_op(p, cells, seen, "avg", 100.0, mine)
    assert full.values == {(0, 0, 0): 200.0, (0, 1, 0): 400.0, (0, 1, 2): 300.0}
    # a pair that is not comparable (other pedal axis, not aligned) gives no step
    c = _tset(["RO", "OR", "RC"], [0, 50], 3)
    q = GE.pair_sets(a, c)
    assert q.axis_differs == {0} and GE.average_op(q, cells, seen, "avg", 50.0, mine) is None

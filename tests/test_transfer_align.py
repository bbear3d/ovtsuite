# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A comparison of the same size on other breakpoints, put onto the primary's axes
(``transfer.build_overlay(align=True)``) -- and left exactly as it was without it.

Without ``align`` such a table is "axis-mismatch": each value stays in the cell it is
stored in, a different operating point. With it each primary cell is compared with the
comparison's value at its own operating point, an axis that matches is not touched, and
an axis of labels (a ZF TCC or shift schedule's gear columns) is never interpolated."""
import math

import pytest

from ovtsuite.transfer import build_overlay


class _T:
    kind = "map"

    def __init__(self, values, x, y):
        self._v, self._x, self._y = values, list(x), list(y)
        self.rows, self.cols = len(values), len(values[0])

    def values(self):
        return [list(r) for r in self._v]

    def x_values(self):
        return list(self._x)

    def y_values(self):
        return list(self._y)

    def cell(self, r, c):
        return self._v[r][c]


def _close(a, b):
    return all(math.isclose(p, q, abs_tol=1e-9) for ra, rb in zip(a, b)
               for p, q in zip(ra, rb))


def test_without_align_the_cells_stay_where_they_are_stored():
    p = _T([[1, 2], [3, 4], [5, 6]], [10, 20], [0, 50, 100])
    o = _T([[10, 20], [30, 40], [50, 60]], [10, 20], [0, 40, 100])
    ov = build_overlay(p, o)
    assert ov.mode == "axis-mismatch" and ov.values == [[10, 20], [30, 40], [50, 60]]


def test_align_puts_the_other_axis_onto_the_primarys():
    p = _T([[1, 2], [3, 4], [5, 6]], [10, 20], [0, 50, 100])
    o = _T([[10, 20], [30, 40], [50, 60]], [10, 20], [0, 40, 100])
    ov = build_overlay(p, o, align=True)
    assert ov.mode == "resampled" and "resampled onto this table's axes" in ov.note
    # Y 50 lies 10/60 of the way from the comparison's 40 to its 100
    assert _close(ov.values, [[10, 20], [30 + 20 / 6, 40 + 20 / 6], [50, 60]])


def test_align_interpolates_x_too_and_edges_are_held():
    p = _T([[0, 0, 0]], [5, 15, 40], [0])
    o = _T([[100, 200, 300]], [10, 20, 30], [0])
    ov = build_overlay(p, o, align=True)
    assert ov.mode == "resampled" and _close(ov.values, [[100, 150, 300]])


def test_matching_axes_are_direct_with_or_without_align():
    p = _T([[1, 2]], [1, 2], [0])
    o = _T([[5, 6]], [1, 2], [0])
    assert build_overlay(p, o, align=True).mode == "direct"
    assert build_overlay(p, o).values == [[5, 6]]


def test_only_the_axis_that_differs_is_interpolated():
    p = _T([[1, 2], [3, 4]], [1, 2], [0, 50])
    o = _T([[10, 20], [30, 40]], [1, 2], [0, 100])
    ov = build_overlay(p, o, align=True)
    assert _close(ov.values, [[10, 20], [20, 30]])      # Y 50: halfway; X untouched


def test_labelled_columns_are_matched_not_interpolated():
    """A ZF schedule's columns are labels: the comparison's X breakpoints differing must
    not slide its values across them."""
    p = _T([[1, 2, 3], [4, 5, 6]], [1, 2, 3], [0, 50])
    o = _T([[10, 20, 30], [40, 50, 60]], [1.5, 2.5, 3.5], [0, 100])
    ov = build_overlay(p, o, columns=[0, 1, 2], align=True)
    assert ov.mode == "resampled" and _close(ov.values, [[10, 20, 30], [25, 35, 45]])


def test_an_axis_that_cannot_be_interpolated_keeps_the_cell_for_cell_view():
    nan = float("nan")
    p = _T([[1, 2], [3, 4]], [1, 2], [0, 50])
    assert build_overlay(p, _T([[9, 9], [9, 9]], [1, 2], [0, nan]),
                         align=True).mode == "axis-mismatch"
    assert build_overlay(p, _T([[9, 9], [9, 9]], [1, 2], [0, 100]),
                         align=True).mode == "resampled"
    zig = _T([[1, 2], [3, 4], [5, 6]], [1, 2], [0, 100, 50])
    p3 = _T([[1, 2], [3, 4], [5, 6]], [1, 2], [0, 50, 100])
    assert build_overlay(p3, zig, align=True).mode == "axis-mismatch"


def test_a_different_size_is_resampled_as_it_always_was():
    p = _T([[1, 2, 3], [4, 5, 6]], [1, 2, 3], [0, 100])
    o = _T([[10, 20], [30, 40]], [1, 3], [0, 100])
    assert build_overlay(p, o, align=True).values == build_overlay(p, o).values


def test_a_one_dimensional_table_stored_the_other_way_round_is_turned():
    p = _T([[1, 2, 3]], [0, 10, 20], [])
    o = _T([[10], [20], [30]], [], [0, 10, 20])
    ov = build_overlay(p, o, align=True)
    assert ov.mode == "direct" and ov.values == [[10, 20, 30]]

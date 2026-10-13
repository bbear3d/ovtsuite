# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Compare/transfer a parameter between two calibrations of differing shape.

The two binaries are paired by identity, so the *same* parameter can have
different axis breakpoints or a different size between calibrations. To
overlay one on the other, the comparison surface is resampled onto the
primary's axes:

  same dims + same axes   -> "direct" (values used as-is)
  same dims + diff axes    -> "axis-mismatch" (cell-by-cell, flagged)
  different dims           -> "resampled" (bilinear onto primary axes)

Resampling uses the axis *values* (rpm, load, ...) so a primary cell is
compared against the other calibration's value at the same operating point,
clamped to the comparison's axis range at the edges.
"""

from dataclasses import dataclass
from typing import List, Optional

from .model import Entity, values_equal


def _interp1(xs: List[float], ys: List[float], x: float) -> float:
    """Linear interpolation with edge clamping. xs may be ascending or
    descending; must be monotonic."""
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1:
        return ys[0]
    ascending = xs[-1] >= xs[0]
    if not ascending:
        xs = xs[::-1]
        ys = ys[::-1]
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, n - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    if span == 0:
        return ys[lo]
    f = (x - xs[lo]) / span
    return ys[lo] + f * (ys[hi] - ys[lo])


@dataclass
class Overlay:
    mode: str                       # 'direct' | 'axis-mismatch' | 'resampled'
    values: List[List[float]]       # aligned to the primary's (rows, cols)
    note: str = ""


def _axes_equal(a: List[float], b: List[float]) -> bool:
    """Two axes are the same if every breakpoint matches within a decode
    rounding error. ``values_equal`` rather than a bare subtraction because a
    float axis can hold NaN in its unused slots, and ``NaN - NaN`` is NaN --
    which is not < 1e-6, so an axis would fail to match itself and the overlay
    would resample a table that needed no resampling."""
    return len(a) == len(b) and all(values_equal(x, y, 1e-6)
                                    for x, y in zip(a, b))


def build_overlay(primary: Entity, other: Entity,
                  columns: Optional[List[Optional[int]]] = None
                  ) -> Optional[Overlay]:
    """Comparison values aligned to the primary, or None if incomparable.

    ``columns`` overrides the X interpolation with an explicit destination
    column -> source column mapping (``None`` for a destination column the
    source has no value for). It exists because interpolating across X is only
    correct when X is a *measured* quantity. A ZF TCC schedule's columns are
    labels -- ten gear-shift groups, each with three or four sub-columns -- so
    a 30-column map laid over a 40-column one must line up group by group and
    leave the absent fourth sub-column empty. Interpolated instead, every
    value slides by three quarters of a column and the error grows across the
    map: on the 2.0 TFSI over the 3.0 TFSI, "1>2 Full Opened" came out holding
    the 30-column map's "2>3 Start of Closed" value.
    """
    if other is None or primary.kind == "constant":
        if primary.kind == "constant" and other is not None:
            return Overlay("direct", [[other.cell(0, 0)]])
        return None

    pv = primary.values()
    ov = other.values()
    px, py = primary.x_values(), primary.y_values()
    ox, oy = other.x_values(), other.y_values()
    orows, ocols = other.rows, other.cols

    # A one-dimensional table is the same run of values whichever way its
    # definition happens to store it, Nx1 or 1xN -- the reference files use
    # both for one parameter. Turn the comparison to the primary's way round
    # before anything else: left as it was, a column was "resampled" across
    # a row, and the first value smeared over every cell (a 12-character
    # part number compared as twelve copies of its first character).
    p1d = 1 in (primary.rows, primary.cols)
    o1d = 1 in (orows, ocols)
    if (p1d and o1d and (primary.rows, primary.cols) != (1, 1)
            and (orows, ocols) != (1, 1)
            and (primary.rows == 1) != (orows == 1)):
        ov = [list(col) for col in zip(*ov)]
        ox, oy = oy, ox
        orows, ocols = ocols, orows

    if (primary.rows, primary.cols) == (orows, ocols):
        same = (_axes_equal(px, ox) or not px or not ox) and \
               (_axes_equal(py, oy) or not py or not oy)
        if same:
            return Overlay("direct", ov)
        return Overlay("axis-mismatch", ov,
                       "axes differ between calibrations")

    if columns is not None:
        # Labelled columns: matched, never interpolated. Rows still are --
        # the Y axis is accelerator position, a real quantity, and the two
        # calibrations put their pedal points in different places.
        if not oy:
            oy = list(range(orows))
        if not py:
            py = list(range(primary.rows))
        out = []
        for r in range(primary.rows):
            yv = py[r] if r < len(py) else r
            row = []
            for c in range(primary.cols):
                sc = columns[c] if c < len(columns) else None
                if sc is None or sc >= ocols:
                    row.append(None)
                    continue
                col = [ov[rr][sc] for rr in range(orows)]
                row.append(_interp1(oy, col, yv))
            out.append(row)
        missing = sum(1 for c in columns if c is None)
        return Overlay(
            "resampled", out,
            "comparison matched column by column from %dx%d%s"
            % (other.rows, other.cols,
               "; %d columns the comparison does not have" % missing
               if missing else ""))

    # different shape -> resample onto the primary's axes
    if not ox:
        ox = list(range(ocols))
    if not oy:
        oy = list(range(orows))
    if not px:
        px = list(range(primary.cols))
    if not py:
        py = list(range(primary.rows))

    out: List[List[float]] = []
    for r in range(primary.rows):
        yv = py[r] if r < len(py) else r
        # interpolate each comparison row to this y, then across x
        row_at_y = []
        for c in range(ocols):
            col = [ov[rr][c] for rr in range(orows)]
            row_at_y.append(_interp1(oy, col, yv))
        out_row = []
        for c in range(primary.cols):
            xv = px[c] if c < len(px) else c
            out_row.append(_interp1(ox, row_at_y, xv))
        out.append(out_row)
    return Overlay("resampled", out,
                   "comparison resampled from %dx%d onto this map's axes"
                   % (other.rows, other.cols))

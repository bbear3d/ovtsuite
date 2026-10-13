# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Interpolate a calibration table the way the ECU does, and split a sample
between the cells it would have read.

EDC16 tables clamp at their axis ends and interpolate linearly in between.
Axes are often padded with near-duplicate break-points at the end (``4700,
4701, 4702 ...``) to fill a fixed-size table; the padding is treated as the end
of the axis so a sample never gets spread over filler columns.

Orientation follows :class:`~ovtsuite.model.Entity`: ``row`` indexes the Y
axis, ``col`` the X axis; a curve is one row along X.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import List, Sequence, Tuple


def effective_len(axis: Sequence[float]) -> int:
    """Length of the strictly-increasing part of an axis (padding cut off)."""
    n = len(axis)
    if n <= 1:
        return n
    k = 1
    while k < n and axis[k] >= axis[k - 1]:     # repeated break-points are a step
        k += 1
    # Trailing filler break-points (+1 steps on an axis thousands wide) are
    # padding, not resolution: cut them so the table ends at its real edge.
    span = axis[k - 1] - axis[0]
    tiny = min(1.0, 0.01 * span) if span > 0 else 0.0
    while k > 2 and (axis[k - 1] - axis[k - 2]) <= tiny:
        k -= 1
    return k


def _finite(v) -> bool:
    return v is not None and v == v and v not in (float("inf"), float("-inf"))


def position(axis: Sequence[float], v: float) -> Tuple[int, int, float]:
    """``(i0, i1, frac)`` of ``v`` on an ascending axis, clamped at the ends."""
    n = effective_len(axis)
    if n == 0 or not _finite(v):
        return 0, 0, 0.0
    if n == 1 or v <= axis[0]:
        return 0, 0, 0.0
    if v >= axis[n - 1]:
        return n - 1, n - 1, 0.0
    j = bisect_right(axis, v, 0, n)
    i0, i1 = j - 1, min(j, n - 1)
    span = axis[i1] - axis[i0]
    f = (v - axis[i0]) / span if span > 0 else 0.0
    return i0, i1, f


def weights(entity, x: float, y: float = None) -> List[Tuple[int, int, float]]:
    """``[(row, col, weight)]`` -- the cells a lookup at ``(x, y)`` blends."""
    xs = entity.x_values() if entity.x is not None else [0.0]
    c0, c1, fx = position(xs, x)
    if entity.kind == "curve" or entity.rows == 1 or entity.y is None or y is None:
        out = [(0, c0, 1.0 - fx)]
        if c1 != c0:
            out.append((0, c1, fx))
        return [w for w in out if w[2] > 0]
    ys = entity.y_values()
    r0, r1, fy = position(ys, y)
    out = []
    for r, wy in ((r0, 1.0 - fy), (r1, fy)):
        for c, wx in ((c0, 1.0 - fx), (c1, fx)):
            out.append((r, c, wy * wx))
    merged = {}
    for r, c, w in out:
        merged[(r, c)] = merged.get((r, c), 0.0) + w
    return [(r, c, w) for (r, c), w in merged.items() if w > 1e-9]


def lookup(entity, x: float, y: float = None, values=None) -> float:
    """Table value at ``(x, y)``; ``values`` overrides the stored cells."""
    vals = values if values is not None else entity.values()
    return sum(vals[r][c] * w for r, c, w in weights(entity, x, y))


def axis_max(entity, which: str = "y") -> float:
    ax = entity.y_values() if which == "y" else entity.x_values()
    n = effective_len(ax)
    return ax[n - 1] if n else 0.0

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measure structural-scan coverage against a reference XDF (ground truth)."""

from dataclasses import dataclass, field
from typing import List

from .structure import Found, scan, enumerate_all, find_cal_region
from .xdf import XdfDef


@dataclass
class Coverage:
    exhaustive_exact: int = 0    # found by exhaustive enumeration

    expected: int = 0            # XDF maps+curves with embedded axes
    exact: int = 0               # scanner found the struct at the exact addr
    shape_match: int = 0         # ...and with the same dims
    missed: List[str] = field(default_factory=list)
    detections: int = 0          # total scanner detections
    matched_detections: int = 0  # detections that coincide with an XDF struct
    region: tuple = (0, 0)

    @property
    def recall(self):
        return self.exact / self.expected if self.expected else 0.0

    @property
    def precision(self):
        return (self.matched_detections / self.detections
                if self.detections else 0.0)


def measure(data: bytes, xdf: XdfDef, region=None) -> Coverage:
    cov = Coverage()
    if region is None:
        region = find_cal_region(data)
    cov.region = region
    found = scan(data, region[0], region[1])
    cov.detections = len(found)
    by_addr = {f.addr: f for f in found}
    exh = {f.addr for f in enumerate_all(data, region[0], region[1])}

    for t in xdf.tables:
        sa = t.struct_addr
        if sa is None:
            continue
        # only 16-bit-axis structures are scannable in this pass
        axes = [a for a in (t.x, t.y) if a is not None and a.addr is not None]
        if any(a.bits != 16 for a in axes):
            continue
        cov.expected += 1
        # Two verified map layouts:
        #   standard:   [Nx][Ny][X][Y][Z]      -> one struct at first_axis-4
        #   group/GMAP: [Nx][X...][Ny][Y...]   -> two counted-axis blocks
        #               (Z planes stored elsewhere)
        alt = None
        if t.kind == "map":
            alt = tuple(sorted(a.addr - 2 for a in axes))
        hit_addrs = None
        if sa in by_addr or (alt and all(a in by_addr for a in alt)):
            hit_addrs = by_addr
        if sa in exh or (alt and all(a in exh for a in alt)):
            cov.exhaustive_exact += 1
        if hit_addrs is None:
            if not (sa in exh or (alt and all(a in exh for a in alt))):
                cov.missed.append("%s @0x%X (%s %dx%d)" % (
                    t.name, sa, t.kind, t.rows, t.cols))
            continue
        cov.exact += 1
        f = by_addr.get(sa)
        if f is not None and t.kind == "map":
            ok = (f.kind == "map" and (f.rows, f.cols) == (t.rows, t.cols)
                  or (f.rows, f.cols) == (t.cols, t.rows))
        elif f is not None:
            ok = f.cols == max(t.rows, t.cols)
        else:
            ok = True  # counted-axis pair: counts verified by the match itself
        if ok:
            cov.shape_match += 1

    known = set()
    for t in xdf.tables:
        sa = t.struct_addr
        if sa is not None:
            known.add(sa)
    cov.matched_detections = sum(1 for f in found if f.addr in known)
    return cov

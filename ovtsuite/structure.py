# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Structural detection of EDC16 calibration entities.

Verified storage layout (EDC16U31 P350/P475 and EDC16U1, all big-endian):

  curve:  [u16 N]           [N x u16 axis, strictly monotonic] [data ...]
  map:    [u16 Nx] [u16 Ny] [Nx x u16 X axis] [Ny x u16 Y axis] [Z ...]

Scalars/constants and label-indexed tables carry no header and cannot be
found structurally (same finding as the 6HP project) -- they come from the
reference knowledge base via anchor matching instead.

The scanner walks the calibration region greedily: at each free offset it
tries a map header first, then a curve header, and jumps past accepted
structures (Z assumed 16-bit when unknown -- refined later by the KB).
"""

from dataclasses import dataclass
from typing import List, Optional
import struct


MIN_AXIS = 2          # smallest real axis seen in the references
MAX_AXIS = 64


@dataclass
class Found:
    kind: str          # 'map' | 'curve'
    addr: int          # header address (count word)
    rows: int          # Ny for maps, 1 for curves
    cols: int          # Nx / N
    x_addr: int = 0
    y_addr: int = 0
    z_addr: int = 0

    @property
    def n_cells(self):
        return self.rows * self.cols


def _mono1(vals) -> bool:
    if len(vals) < 2:
        return False
    inc = all(a < b for a, b in zip(vals, vals[1:]))
    dec = all(a > b for a, b in zip(vals, vals[1:]))
    return inc or dec


def _mono(vals) -> bool:
    """Strictly monotonic in either direction, as unsigned or signed 16-bit.

    Axes holding temperatures, torques or accelerations are signed
    (0xFC18 = -1000 sorts before 0), so both interpretations are tried.
    """
    if _mono1(vals):
        return True
    signed = [v - 0x10000 if v >= 0x8000 else v for v in vals]
    return _mono1(signed)


def find_cal_region(data: bytes, bucket_bits: int = 14,
                    threshold: int = 20) -> Optional[tuple]:
    """Locate the calibration area by curve-header density.

    Returns ``(start, end)`` rounded to bucket boundaries, or None.
    U31 images -> ~0x1C0000+, U1 (1MB) -> ~0xD0000+.
    """
    from collections import Counter
    hits = Counter()
    n = len(data)
    for a in range(0, n - 2 * (MAX_AXIS + 1), 2):
        cnt = (data[a] << 8) | data[a + 1]
        if not 3 <= cnt <= 40:
            continue
        vals = struct.unpack_from(">%dH" % cnt, data, a + 2)
        if _mono(vals):
            hits[a >> bucket_bits] += 1
    dense = sorted(b for b, c in hits.items() if c >= threshold)
    if not dense:
        return None
    # Code and other regions produce isolated dense buckets; the calibration
    # area is the longest *contiguous* run (small gaps allowed -- constant
    # blocks between map blocks can empty a bucket).
    runs = []
    run = [dense[0], dense[0]]
    for b in dense[1:]:
        if b - run[1] <= 2:
            run[1] = b
        else:
            runs.append(run)
            run = [b, b]
    runs.append(run)
    # Equal-length runs are decided by how many headers they hold: on some
    # 03G906021 images (HK 2854, TQ 2186) the variant-coding block at
    # 0x184000 spans exactly as many buckets as the main calibration at
    # 0x1C0000, and taking the first of the two cut the main area off.
    best = max(runs, key=lambda r: (r[1] - r[0],
                                    sum(hits[b] for b in range(r[0], r[1] + 1))))
    # Pad one bucket each side: the outermost blocks of the calibration area
    # can be too sparse to clear the density threshold on their own.
    start = max(0, (best[0] - 1) << bucket_bits)
    end = min(len(data), (best[1] + 2) << bucket_bits)
    return (start, end)


def try_map(data: bytes, a: int) -> Optional[Found]:
    if a + 4 > len(data):
        return None
    nx = (data[a] << 8) | data[a + 1]
    ny = (data[a + 2] << 8) | data[a + 3]
    if not (MIN_AXIS <= nx <= MAX_AXIS and MIN_AXIS <= ny <= MAX_AXIS):
        return None
    need = a + 4 + 2 * (nx + ny)
    if need > len(data):
        return None
    x = struct.unpack_from(">%dH" % nx, data, a + 4)
    y = struct.unpack_from(">%dH" % ny, data, a + 4 + 2 * nx)
    if not (_mono(x) and _mono(y)):
        return None
    return Found("map", a, rows=ny, cols=nx,
                 x_addr=a + 4, y_addr=a + 4 + 2 * nx, z_addr=need)


def try_curve_f32(data: bytes, a: int) -> Optional[Found]:
    """Curve with a 32-bit IEEE-754 float axis: [u16 N][N x f32][data].

    Some ASMod/thermal entities store float axes (e.g. 5.0, 10.0, 20.0 ...).
    """
    if a + 2 > len(data):
        return None
    n = (data[a] << 8) | data[a + 1]
    if not MIN_AXIS <= n <= MAX_AXIS:
        return None
    if a + 2 + 4 * n > len(data):
        return None
    vals = struct.unpack_from(">%df" % n, data, a + 2)
    if any(v != v or abs(v) > 1e12 for v in vals):
        return None
    if not _mono1(list(vals)):
        return None
    return Found("curve", a, rows=1, cols=n,
                 x_addr=a + 2, z_addr=a + 2 + 4 * n)


def try_curve(data: bytes, a: int) -> Optional[Found]:
    if a + 2 > len(data):
        return None
    n = (data[a] << 8) | data[a + 1]
    if not MIN_AXIS <= n <= MAX_AXIS:
        return None
    if a + 2 + 2 * n > len(data):
        return None
    vals = struct.unpack_from(">%dH" % n, data, a + 2)
    if not _mono(vals):
        return None
    return Found("curve", a, rows=1, cols=n,
                 x_addr=a + 2, z_addr=a + 2 + 2 * n)


def enumerate_all(data: bytes, start: int, end: int,
                  step: int = 1) -> List[Found]:
    """Exhaustive candidate enumeration -- every offset, no jumping.

    Recall-oriented: catches structures the greedy walk overshoots (8-bit Z
    data breaks its 16-bit jump assumption) and byte-aligned 8-bit entities.
    Produces false positives inside large Z blocks; the knowledge-base
    anchoring pass is what filters those.
    """
    out: List[Found] = []
    for a in range(start, end, step):
        f = try_map(data, a)
        if f is not None:
            out.append(f)
            continue
        f = try_curve(data, a)
        if f is not None:
            out.append(f)
    return out


def scan(data: bytes, start: int, end: int,
         z_bits_guess: int = 16) -> List[Found]:
    """Greedy sequential scan of ``data[start:end]``."""
    out: List[Found] = []
    a = start & ~1
    zstep = z_bits_guess // 8
    while a < end:
        f = try_map(data, a)
        if f is not None:
            out.append(f)
            a = f.z_addr + zstep * f.n_cells
            a += a & 1
            continue
        f = try_curve(data, a)
        if f is not None:
            out.append(f)
            a = f.z_addr + zstep * f.cols
            a += a & 1
            continue
        a += 2
    return out

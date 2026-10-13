# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Structural detection of ZF 6HP calibration entities.

One scanner, parameterised by element width. The P2a survey
(``docs/OVT_SUITE_PORT.md`` §3.1) measured every storage class against both
known-good definition/binary pairs and found the header on 100 % of maps and
curves in each, with the two map axes always adjacent and Z always beginning
where the axes end:

    curve:  [u16 N ][ N axis values ][ N data values ]
    map:    [u16 Nx][u16 Ny][ Nx X ][ Ny Y ][ Nx*Ny Z ]

An 8-bit table is that same structure with a narrower payload -- the count
header stays 16-bit -- so it is a width parameter here, not the separate
scanner with its own thresholds that hp6tuner grew.

Two things this deliberately does NOT do, both lessons from the survey:

* **No shape whitelist.** hp6tuner lists 38 permitted ``(cols, rows)`` pairs;
  the two definitions between them use 39, and a whitelist cannot generalise
  to the next calibration. Any shape within the measured bounds is accepted.
* **No attempt at precision on its own.** A permissive scanner is correct here
  because the knowledge base decides what survives (``match6hp``), which is
  how the EDC16 side already works. Scanning is for recall.

Label-indexed arrays and scalars have no header and are not detectable
structurally; they come from the knowledge base.
"""

import math
import struct
from dataclasses import dataclass
from typing import List, Optional

#: Measured across both definitions: curve N is 2..12, map axes are 2..40.
#: The bound is the union plus nothing -- widening it only buys false
#: positives, and the knowledge base is what turns recall into precision.
MIN_N = 2
MAX_CURVE_N = 12
MAX_MAP_N = 40

_WIDTH = {8: 1, 16: 2, 32: 4}

#: Order the widths are tried in. 32 is last on purpose: it is a different
#: payload type rather than a different size, and putting it after the integer
#: widths means it can only ever claim an address the established readings
#: have already refused.
WIDTHS = (16, 8, 32)

#: A 32-bit float that is neither absurd nor NaN. The bound is loose because
#: these parameters carry everything from 0.03 (a ratio) to 6016 (an engine
#: speed); its job is to reject code and pointers read as floats, which come
#: out as 1e-38 or 1e30, not to express a belief about the values.
_F_MIN, _F_MAX = 1e-6, 1e9


@dataclass
class Found6:
    kind: str            # 'map' | 'curve'
    addr: int            # header address (the count word)
    rows: int            # Ny for maps, 1 for curves
    cols: int            # Nx / N
    x_addr: int = 0
    y_addr: int = 0
    z_addr: int = 0
    bits: int = 16       # element width of the axes and Z
    #: For the shift/TCC families, the first value of the index run that
    #: stands in for an X axis (1 for shift schedules, 0 for TCC). None when
    #: the X axis holds real breakpoints.
    index_base: Optional[int] = None

    @property
    def n_cells(self) -> int:
        return self.rows * self.cols

    @property
    def end(self) -> int:
        return self.z_addr + self.n_cells * (self.bits // 8)


def _read(data: bytes, addr: int, count: int, bits: int):
    if bits == 8:
        return list(data[addr:addr + count])
    if bits == 32:
        return list(struct.unpack_from(">%df" % count, data, addr))
    return list(struct.unpack_from(">%dH" % count, data, addr))


def _sane(vals) -> bool:
    """Every value a number this format plausibly stores. 32-bit only.

    The integer widths need no such test -- every bit pattern is a legal
    value -- but a float read out of code or a pointer table is usually NaN,
    an infinity or a denormal, and rejecting those is most of what keeps the
    float scan from inventing structures.
    """
    for v in vals:
        if not math.isfinite(v):
            return False
        if v != 0.0 and not (_F_MIN < abs(v) < _F_MAX):
            return False
    return True


def _mono(vals, bits: int) -> bool:
    """Strictly monotonic in either direction, unsigned or signed.

    Axes holding temperatures, accelerations or torques are signed -- an
    axis running -20, -10, 0, 10, 20 reads as 65516, 65526, 0, 10, 20
    unsigned, which is not monotonic -- so both interpretations are tried.
    Floats are already signed, so for them the question is asked once.
    """
    if len(vals) < 2:
        return False
    if bits == 32:
        return (all(a < b for a, b in zip(vals, vals[1:]))
                or all(a > b for a, b in zip(vals, vals[1:])))
    for v in (vals, [x - (1 << bits) if x >= 1 << (bits - 1) else x for x in vals]):
        if all(a < b for a, b in zip(v, v[1:])) or all(a > b for a, b in zip(v, v[1:])):
            return True
    return False


def try_curve(data: bytes, a: int, bits: int = 16) -> Optional[Found6]:
    """``[u16 N][N axis][N data]`` at ``a``, or None."""
    if a + 2 > len(data):
        return None
    n = struct.unpack_from(">H", data, a)[0]
    if not MIN_N <= n <= MAX_CURVE_N:
        return None
    w = _WIDTH[bits]
    x_addr = a + 2
    z_addr = x_addr + n * w
    if z_addr + n * w > len(data):
        return None
    axis = _read(data, x_addr, n, bits)
    if not _mono(axis, bits):
        return None
    if bits == 32 and not (_sane(axis) and _sane(_read(data, z_addr, n, bits))):
        return None
    return Found6("curve", a, rows=1, cols=n, x_addr=x_addr, z_addr=z_addr, bits=bits)


def try_map(data: bytes, a: int, bits: int = 16) -> Optional[Found6]:
    """``[u16 Nx][u16 Ny][X][Y][Z]`` at ``a``, or None."""
    if a + 4 > len(data):
        return None
    nx, ny = struct.unpack_from(">HH", data, a)
    if not (MIN_N <= nx <= MAX_MAP_N and MIN_N <= ny <= MAX_MAP_N):
        return None
    w = _WIDTH[bits]
    x_addr = a + 4
    y_addr = x_addr + nx * w
    z_addr = y_addr + ny * w
    if z_addr + nx * ny * w > len(data):
        return None
    x, y = _read(data, x_addr, nx, bits), _read(data, y_addr, ny, bits)
    if not (_mono(x, bits) and _mono(y, bits)):
        return None
    if bits == 32 and not (_sane(x) and _sane(y)
                           and _sane(_read(data, z_addr, nx * ny, bits))):
        return None
    return Found6("map", a, rows=ny, cols=nx, x_addr=x_addr, y_addr=y_addr,
                  z_addr=z_addr, bits=bits)


def try_shift_map(data: bytes, a: int, bits: int = 16) -> Optional[Found6]:
    """``[u16 cols][u16 rows][cols index values][rows Y][rows*cols Z]`` at ``a``.

    The third ZF form, and the one the tuning families use. Where a normal map
    stores real X breakpoints, a shift schedule stores a RUN OF INDICES --
    1..cols for the shift families, 0..cols-1 for the torque-converter ones --
    because its X axis is a set of labels (``1>2``, ``2>3`` ...) rather than a
    measured quantity. The definition therefore carries no X address at all,
    which is why ``try_map`` cannot see these: the index run occupies the space
    an X axis would, and the Y axis follows it.

    Detected by the index run itself, which is unambiguous: ``cols``
    consecutive integers starting at 0 or 1, immediately after a header whose
    first word equals their count.
    """
    if a + 4 > len(data):
        return None
    cols, rows = struct.unpack_from(">HH", data, a)
    if not (MIN_N <= cols <= MAX_MAP_N and MIN_N <= rows <= MAX_MAP_N):
        return None
    w = _WIDTH[bits]
    run_addr = a + 4
    y_addr = run_addr + cols * w
    z_addr = y_addr + rows * w
    if z_addr + rows * cols * w > len(data):
        return None
    run = _read(data, run_addr, cols, bits)
    base = run[0]
    if base not in (0, 1) or run != list(range(base, base + cols)):
        return None
    # the Y axis is a real axis (pedal position, turbine speed ...)
    if not _mono(_read(data, y_addr, rows, bits), bits):
        return None
    f = Found6("map", a, rows=rows, cols=cols, x_addr=run_addr, y_addr=y_addr,
               z_addr=z_addr, bits=bits)
    f.index_base = base
    return f


def try_any(data: bytes, a: int) -> Optional[Found6]:
    """The best structure at ``a``: a map before a curve, 16-bit before 8-bit.

    Kind order matters. A shift map is a map whose X axis is an index run, so
    it is tried first and the plain map test only sees what it rejects; and a
    map's header is a superset of a curve's, so a curve test would match the
    ``Nx`` of a map and stop short.

    Element width is genuinely ambiguous and the order does NOT settle it. A
    short 8-bit curve reads as a valid 16-bit one (bytes ``04 0A 14 1E`` are
    the monotonic u16 pair 0x040A, 0x141E), so 72 of 1948 tables come back at
    the right address with the wrong width. Trying 8-bit first was measured on
    both known-good pairs and is no better -- 96.1 % against 96.3 % exact
    geometry -- because it simply swaps which tables are misread. Width comes
    from the knowledge base; what the scanner is for is finding the address.

    **32-bit floats are a real third payload**, not a third size: 165
    parameters in each definition are stored that way and were invisible to
    this scanner until the OEM-corpus diff turned them up as bytes that
    demonstrably vary and that nothing claimed. They use the same grammar --
    96 of the 165 sit behind exactly the ``[u16 cols][u16 rows]`` map header
    the other widths use, the same 96 on both known-good pairs -- so they need
    no scanner of their own, only the width.

    Two deliberate limits. The shift form is not tried at 32 bits: no shift or
    TCC schedule in either definition is anything but 16-bit, and a float
    index run is a false positive waiting to happen. And the remaining 69 are
    left unclaimed: a first pass appeared to find 34 of them behind a curve
    header, and every one was a coincidence -- four zero bytes read as the
    word pair ``0, N``, with an all-zero axis and a payload of NaNs and
    -1e8 pointer bit patterns behind it. ``_sane`` is what rejects them, which
    is why the payload test matters more here than the header does.
    """
    for fn in (try_shift_map, try_map, try_curve):
        for bits in WIDTHS:
            if bits == 32 and fn is try_shift_map:
                continue
            f = fn(data, a, bits)
            if f is not None:
                return f
    return None


def scan(data: bytes, start: int, end: int, step: int = 2) -> List[Found6]:
    """Greedy sequential scan: accept a structure, jump past it, continue.

    ``step`` 2 follows the 16-bit alignment every structure in the references
    observes; pass 1 to also catch odd-aligned 8-bit tables, at the cost of
    roughly twice the false positives for the knowledge base to reject.
    """
    out: List[Found6] = []
    a = start - (start % step)
    while a < end:
        f = try_any(data, a)
        if f is None:
            a += step
            continue
        out.append(f)
        a = max(f.end, a + step)
        if a % step:
            a += step - (a % step)
    return out


def enumerate_all(data: bytes, start: int, end: int, step: int = 2) -> List[Found6]:
    """Every offset, no jumping -- recall-oriented, for measuring coverage.

    The greedy walk overshoots anything nested inside a larger structure's
    payload; this finds those too, at the price of many false positives.
    """
    out: List[Found6] = []
    for a in range(start - (start % step), end, step):
        f = try_any(data, a)
        if f is not None:
            out.append(f)
    return out

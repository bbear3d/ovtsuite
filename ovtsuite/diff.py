# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Named, physical-unit diff of two binaries of the same layout.

Every differing byte range is attributed to the identified entity covering
it; ranges outside every entity are reported as undocumented gaps (the
hp6tuner undocumented-differences idea).
"""

import bisect
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .equation import Equation
from .match import MatchResult, Identified


@dataclass
class EntityDiff:
    name: str
    kind: str
    units: str
    addr: int              # entity data address (in these binaries)
    changed_cells: int = 0
    total_cells: int = 0
    old_vals: list = field(default_factory=list)   # display units, changed cells
    new_vals: list = field(default_factory=list)
    cell_idx: list = field(default_factory=list)
    source: str = ""
    description: str = ""


@dataclass
class DiffResult:
    entities: List[EntityDiff] = field(default_factory=list)
    gaps: List[Tuple[int, int]] = field(default_factory=list)  # [start,end)
    diff_bytes: int = 0
    gap_bytes: int = 0


def _byte_ranges(a: bytes, b: bytes, start: int, end: int):
    """Contiguous [start,end) ranges where a and b differ."""
    out = []
    i = start
    n = min(len(a), len(b), end)
    while i < n:
        if a[i] != b[i]:
            j = i + 1
            while j < n and a[j] != b[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def _spans(items: List[Identified]):
    """Sorted (start, end, item) spans of entity data in the target."""
    spans = []
    for it in items:
        if it.kb_table is not None:
            t = it.kb_table
            w = t.z_bits // 8
            n = t.rows * t.cols
            start = it.target_addr
            end = start + n * w
            # include the storage structure (counts + axes) when contiguous
            if t.struct_addr is not None:
                lead = t.z_addr - t.struct_addr
                if 0 < lead <= 4 + 2 * (t.x_count + t.y_count) + 4:
                    start -= lead
            spans.append((start, end, it))
        elif it.kb_constant is not None:
            c = it.kb_constant
            spans.append((it.target_addr, it.target_addr + c.bits // 8, it))
    spans.sort(key=lambda s: s[0])
    return spans


def _read(data: bytes, addr: int, bits: int, signed: bool) -> int:
    w = bits // 8
    v = int.from_bytes(data[addr:addr + w], "big")
    if signed and v >= 1 << (bits - 1):
        v -= 1 << bits
    return v


def diff(a: bytes, b: bytes, ident: MatchResult,
         region: Tuple[int, int]) -> DiffResult:
    res = DiffResult()
    ranges = _byte_ranges(a, b, region[0], region[1])
    res.diff_bytes = sum(e - s for s, e in ranges)
    spans = _spans(ident.items)
    starts = [s[0] for s in spans]

    touched = {}
    for rs, re_ in ranges:
        pos = rs
        while pos < re_:
            i = bisect.bisect_right(starts, pos) - 1
            hit = None
            while i >= 0 and i < len(spans):
                s, e, it = spans[i]
                if s <= pos < e:
                    hit = (s, e, it)
                    break
                if s <= pos:
                    break
                i -= 1
            if hit is None:
                # extend to end of range or next span start
                nxt = re_
                j = bisect.bisect_right(starts, pos)
                if j < len(starts):
                    nxt = min(nxt, starts[j])
                if res.gaps and res.gaps[-1][1] == pos:
                    res.gaps[-1] = (res.gaps[-1][0], nxt)
                else:
                    res.gaps.append((pos, nxt))
                res.gap_bytes += nxt - pos
                pos = nxt
            else:
                s, e, it = hit
                touched.setdefault(id(it), it)
                pos = min(re_, e)

    for it in touched.values():
        if it.kb_table is not None:
            t = it.kb_table
            bits, signed, eqs = t.z_bits, t.z_signed, t.z_equation
            n = t.rows * t.cols
            units = t.units
            desc = t.description
        else:
            c = it.kb_constant
            bits, signed, eqs = c.bits, c.signed, c.equation
            n = 1
            units = c.units
            desc = c.description
        eq = None
        try:
            eq = Equation(eqs)
        except Exception:
            pass
        ed = EntityDiff(it.name, it.kind, units, it.target_addr,
                        total_cells=n, source=it.source, description=desc)
        w = bits // 8
        for i in range(n):
            addr = it.target_addr + i * w
            if a[addr:addr + w] != b[addr:addr + w]:
                ov = _read(a, addr, bits, signed)
                nv = _read(b, addr, bits, signed)
                if eq is not None and eq.linear:
                    ov, nv = eq.decode(ov), eq.decode(nv)
                ed.changed_cells += 1
                if len(ed.old_vals) < 16:
                    ed.old_vals.append(ov)
                    ed.new_vals.append(nv)
                    ed.cell_idx.append(i)
        # axis/structure edits (bytes changed inside the span but not in Z)
        if ed.changed_cells == 0:
            ed.changed_cells = -1     # marker: structure/axis area changed
        res.entities.append(ed)
    res.entities.sort(key=lambda e: e.addr)
    return res


# --- comparing two tables that are not the same shape ----------------------
#
# Split out of the GUI so it can be tested without Qt, and so the patch writer
# can use the same rule.

def _compare_columns(e, cp):
    # imported lazily: speedview reaches the ZF 6HP module's view when it is
    # installed, and diff.py is used on images that never need it
    from .speedview import compare_columns
    return compare_columns(e, cp)


def _build_overlay(e, cp, cols):
    from .transfer import build_overlay
    return build_overlay(e, cp, cols)


def same_extent(e, cp) -> bool:
    """Do the two sides occupy the same number of bytes, laid out alike?

    Only then is a raw byte compare meaningful. A 9x40 TCC map against a 9x30
    one is 720 bytes against 540: unequal slices are never equal, so every such
    table reported as differing whatever it held, and "show only parameters that
    differ" listed all of them on any cross-calibration comparison.
    """
    return (int(e.rows) == int(cp.rows) and int(e.cols) == int(cp.cols)
            and int(e.bits) == int(cp.bits)
            and (int(e.end) - int(e.addr)) == (int(cp.end) - int(cp.addr)))


def _cell_step(e) -> float:
    """Physical size of one raw step for this entity, used as the tolerance.

    The overlay RESAMPLES across the Y axis when the two files' axes differ, so
    its values are not generally representable in a cell: comparing a 9x30 TCC
    map into a 9x40 one produced 949.0196 and 1230.392 against stored 949 and
    1230. Those are resampling artefacts, not edits, and an exact compare called
    them differences. Half a raw step is the smallest change the table can
    actually hold, so it is the right threshold.
    """
    try:
        eq = e._eq or Equation(e.equation or "X")
        step = abs(float(eq.decode(1)) - float(eq.decode(0)))
    except Exception:
        step = 1.0
    return step if step > 0 else 1.0


def cells_differ(e, cp) -> bool:
    """Compare two differently-shaped tables through the overlay's own mapping.

    The same ``compare_columns`` mapping the grid draws with, so a sub-column the
    comparison does not have is absent rather than different -- the 30-column TCC
    maps omit each group's "Full Opened", and counting those as changes is what
    made the whole family look edited.

    Equal to within half a raw step, for the resampling reason in ``_cell_step``.
    """
    try:
        overlay = _build_overlay(e, cp, _compare_columns(e, cp))
    except Exception:
        return True                     # cannot align them: say so rather than lie
    vals = getattr(overlay, "values", None) if overlay else None
    if not vals:
        return True
    tol = _cell_step(e) / 2.0
    for r, row in enumerate(vals):
        for c, other in enumerate(row or ()):
            if other is None:
                continue                # the comparison has no such cell
            try:
                mine = e.cell(r, c)
            except Exception:
                return True
            if mine is None:
                continue
            if abs(float(mine) - float(other)) > tol:
                return True
    return False
from .msblock import MeasuringBlocks
from .gears import GearRatios
from .variant import vss_pulse_settings
from .signals import SignalCatalog, observed_ids, EMPTY_IDS
from functools import lru_cache as _lru_cache
from .signal_names import translate_german as _tr_de

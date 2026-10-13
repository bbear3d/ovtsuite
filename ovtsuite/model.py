# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Runtime entity model: what the GUI edits.

An Entity wraps a KB definition placed into the loaded binary. Dimensions
come from the STORED count words when a structure is present at the placed
address (the audit's 'oversized' finding: the A2L declares reserved maximum
dimensions, the ECU reads the actual size from the headers), falling back
to the declared dims. Edits write through to the BinaryFile in raw units;
values are presented in physical (display) units via the A2L equation.
"""

from dataclasses import dataclass, field
from typing import List, Optional

from .equation import Equation
from .structure import try_curve, try_curve_f32, try_map


def values_equal(a, b, tol: float = 0.0) -> bool:
    """Compare two calibration values, treating two NaNs as equal.

    An unused 32-bit float parameter is commonly 0xFFFFFFFF, which decodes to
    NaN -- and ``NaN != NaN``, so a plain comparison reports two byte-identical
    files as differing at every such slot, and an axis full of them never
    matches itself. EDC16 Z data is integer today, but float axes already exist
    (``Axis.is_float``) and the ZF 6HP definitions carry 96 float maps and 55
    float-Z curves, so this is the comparison to use on decoded values.

    ``tol`` allows an absolute tolerance, for values that survived a decode /
    encode round trip.
    """
    if a is b:
        return True
    try:
        if a != a and b != b:               # both NaN
            return True
        if tol:
            return abs(a - b) <= tol
        return bool(a == b)
    except TypeError:
        return False


@dataclass
class Axis:
    addr: Optional[int] = None
    count: int = 1
    bits: int = 16
    is_float: bool = False
    equation: str = "X"
    units: str = ""
    _eq: Optional[Equation] = None
    #: stated by a definition (None: decide from the values, as before)
    signed: Optional[bool] = None
    #: the points themselves, for an axis a definition gives instead of
    #: storing it in the binary (``addr`` is None then)
    values: Optional[List[float]] = None
    #: a text per point (an enumeration axis), if the definition has them
    labels: Optional[List[str]] = None

    @property
    def eq(self) -> Equation:
        if self._eq is None:
            try:
                self._eq = Equation(self.equation)
            except Exception:
                self._eq = Equation("X")
        return self._eq


#: Placement grades that mean the address was established rather than
#: inferred: matched byte-exactly, read out of a self-describing structure, or
#: confirmed against one. Anything else -- ``projected`` above all, which is
#: the offset model carrying a symbol across calibrations -- is a plausible
#: guess, and a plausible guess is fine to read and to navigate by but is not
#: something to write a tune into without saying so.
TRUSTED_PLACEMENT = ("anchored", "struct", "verified", "placed")


def placement_trusted(entity) -> bool:
    """Was this entity's address established, or is it the offset model's guess?"""
    return getattr(entity, "source", "") in TRUSTED_PLACEMENT


@dataclass
class Entity:
    name: str
    kind: str                 # 'map' | 'curve' | 'indexed' | 'constant'
    tier: int
    categories: List[str]
    description: str
    units: str
    addr: int                 # Z/data address in the loaded binary
    rows: int
    cols: int
    bits: int
    signed: bool
    equation: str
    source: str               # provenance from matching
    x: Optional[Axis] = None
    y: Optional[Axis] = None
    binfile: object = field(default=None, repr=False)
    declared_rows: int = 0
    declared_cols: int = 0
    # Z storage order for maps: 'col' (EDC16: y varies fastest) or 'row'
    # (EDC15: first-axis-major, i.e. the x/column index varies fastest)
    order: str = "col"
    # display title when it differs from the symbol (EDC15: "symbol - English")
    label: str = ""
    # grouping key: the EDC15 dataset ('SG', 'AG1' ...) an entity belongs to
    group: str = ""
    #: Nested tree path, general to specific -- ["Shift maps", "Coding 1",
    #: "S (sport)"]. Distinct from `categories`, which is an unordered set of
    #: memberships from the definition and must not be nested. Empty for
    #: platforms that do not group hierarchically, and the tree then falls
    #: back to the first category exactly as before.
    category_path: List[str] = field(default_factory=list)
    #: "ascii" when the stored bytes are characters, so a part number reads as
    #: 09L927156C rather than as nine numbers. Empty for numeric data.
    text_kind: str = ""
    # bitfield mask for packed coding scalars (0 = whole byte). Used by vss15
    # to read the cowFUN_FGG DA1/DA2 selector bit.
    sel_mask: int = 0
    #: Z data is IEEE-754, not an integer. The ZF 6HP definitions mark this
    #: with the signed flag on a 32-bit entity -- 164 of the 165 32-bit tables
    #: in each reference carry it, and exactly those decode to sensible
    #: values; read as integers they come back as things like 1112014848,
    #: which is 0x42480000, which is 50.0f.
    is_float: bool = False
    # placement evidence in words, from match.grade() -- why this address is
    # believed correct ("pointer-table block 41 (0x1C8A40, 512 bytes, length
    # unchanged): the ECU addresses it as base + fixed displacement"). Shown in
    # the GUI beside the provenance tier; empty for entities placed by anchor.
    note: str = ""
    #: Tuning-catalogue identity, baked into the knowledge base (see
    #: ovtsuite/catalogue.py): plain English name, tuner shorthand, which
    #: member of a numbered set this is and how that index reads ("gear"),
    #: the selector curve that picks between the set's members, and the
    #: operating range that selector gives this member on THIS file.
    plain: str = ""
    short: str = ""
    set_index: str = ""
    idx_kind: str = ""
    selector: str = ""
    sel_kind: str = ""
    set_note: str = ""
    cat_order: int = 0        # position in the catalogue: curated maps sort first
    #: For a coding-variant copy: the coding(s) that select it, from the
    #: active pointer array ("coding 150031 (5-speed manual, manual A/C)").
    coding: str = ""
    #: For a coding-variant copy: the group its main copy is in. The copy
    #: itself sits in "Coding variants"; this is what decides whether it is
    #: behind "Advanced" like its main copy.
    base_tier: int = 0
    _eq: Optional[Equation] = None

    @property
    def eq(self) -> Equation:
        if self._eq is None:
            try:
                self._eq = Equation(self.equation)
            except Exception:
                self._eq = Equation("X")
        return self._eq

    @property
    def title(self) -> str:
        return self.label or self.name

    @property
    def step(self) -> int:
        return self.bits // 8

    @property
    def n_cells(self) -> int:
        return self.rows * self.cols

    @property
    def end(self) -> int:
        return self.addr + self.n_cells * self.step

    def index(self, r: int, c: int) -> int:
        """Cell -> storage index. EDC16 stores map Z data column-major
        (the Y axis varies fastest -- verified by total-variation analysis
        on PCR/FlMng maps); curves and indexed tables are linear."""
        if self.kind == "map" and self.order == "col":
            return c * self.rows + r
        return r * self.cols + c

    # -- raw <-> display -----------------------------------------------
    def _cell_addr(self, r: int, c: int) -> int:
        return self.addr + self.index(r, c) * self.step

    def raw_cell(self, r: int, c: int):
        """The stored value, before the equation. A float for IEEE-754 Z."""
        if self.is_float:
            import struct as _s
            en = getattr(self.binfile, "endian", ">")
            return _s.unpack_from(en + "f", self.binfile.data,
                                  self._cell_addr(r, c))[0]
        return self.binfile.read(self._cell_addr(r, c), self.bits, self.signed)

    def cell(self, r: int, c: int) -> float:
        return self.eq.decode(self.raw_cell(r, c))

    def values(self) -> List[List[float]]:
        return [[self.cell(r, c) for c in range(self.cols)]
                for r in range(self.rows)]

    def set_cell(self, r: int, c: int, display_value: float) -> float:
        """Write one cell (display units). Returns the clamped value."""
        if self.is_float:
            import struct as _s
            en = getattr(self.binfile, "endian", ">")
            val = float(self.eq.encode(display_value))
            _s.pack_into(en + "f", self.binfile.data,
                         self._cell_addr(r, c), val)
            return self.eq.decode(val)
        raw = int(round(self.eq.encode(display_value)))
        lo = -(1 << (self.bits - 1)) if self.signed else 0
        hi = (1 << (self.bits - 1)) - 1 if self.signed \
            else (1 << self.bits) - 1
        raw = max(lo, min(hi, raw))
        enc = raw + (1 << self.bits) if (self.signed and raw < 0) else raw
        self.binfile.write(self.addr + self.index(r, c) * self.step,
                           enc, self.bits)
        return self.eq.decode(raw)

    @property
    def raw_range(self) -> tuple:
        """``(low, high)`` of the STORED value, before the equation."""
        if self.is_float:
            return (-3.4e38, 3.4e38)
        if self.signed:
            half = 1 << (self.bits - 1)
            return (-half, half - 1)
        return (0, (1 << self.bits) - 1)

    @property
    def value_range(self) -> tuple:
        """``(low, high)`` in DISPLAY units.

        Decoded through the equation and then ordered, because a negative
        slope -- ``(X)*(-0.1)+100`` and friends -- puts the storage maximum at
        the bottom of the displayed range. hp6tuner could return the raw range
        directly since its maps carried no equation; here the two differ for
        almost every parameter.
        """
        lo, hi = self.raw_range
        a, b = self.eq.decode(lo), self.eq.decode(hi)
        return (a, b) if a <= b else (b, a)

    def clamp(self, value: float) -> float:
        """``value`` as this cell would actually hold it.

        Both the range and the quantisation: an integer cell rounds, so a plan
        that asks for 1649.55 records 1650 and never claims a write it cannot
        perform. Returned in display units, like everything else on Entity.
        """
        low, high = self.value_range
        value = max(low, min(high, float(value)))
        if self.is_float:
            return value
        raw = int(round(self.eq.encode(value)))
        rlo, rhi = self.raw_range
        return self.eq.decode(max(rlo, min(rhi, raw)))

    def axis_values(self, ax: Axis) -> List[float]:
        if ax is None:
            return []
        if ax.addr is None:
            return list(ax.values or [])
        if ax.is_float:
            import struct as _s
            en = getattr(self.binfile, "endian", ">")
            raw = [_s.unpack_from(en + "f", self.binfile.data,
                                  ax.addr + 4 * i)[0]
                   for i in range(ax.count)]
        elif ax.signed is not None:
            raw = self.binfile.read_many(ax.addr, ax.count, ax.bits, ax.signed)
        else:
            raw = self.binfile.read_many(ax.addr, ax.count, ax.bits, True)
            # axes are signed when they wrap; use signed decode only when
            # it makes the sequence monotonic
            un = self.binfile.read_many(ax.addr, ax.count, ax.bits, False)
            if _is_mono(un):
                raw = un
        return [ax.eq.decode(v) for v in raw]

    def x_values(self) -> List[float]:
        return self.axis_values(self.x)

    def y_values(self) -> List[float]:
        return self.axis_values(self.y)


def _is_mono(vals) -> bool:
    if len(vals) < 2:
        return True
    return (all(a < b for a, b in zip(vals, vals[1:]))
            or all(a > b for a, b in zip(vals, vals[1:])))

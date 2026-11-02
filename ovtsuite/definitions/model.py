# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""One description of a calibration's parameters, whatever file it came from.

Every definition format the base app reads -- TunerPro XDF, ASAP2 (A2L),
WinOLS projects (OLS) and map packs (KP), Bosch DAMOS (DAM) -- is parsed into
this model, and :mod:`ovtsuite.xdf_session` turns it into the session's
entities. Everything here is what the source file itself says: an address,
a storage width, a sign, a byte order, a conversion. Nothing is inferred from
the binary's contents.

Addresses are *definition addresses*; ``Definition.base`` is subtracted to
get an offset in the image (0 when the file already uses file offsets, as
WinOLS does; an A2L uses ECU memory addresses, resolved against its memory
segments when it is applied).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

#: table kinds
KINDS = ("constant", "curve", "map")


class DefinitionError(ValueError):
    """The file is not a definition this module can read."""


@dataclass
class Storage:
    """How a run of values is stored and converted."""
    addr: Optional[int] = None      # definition address (None: not stored)
    bits: int = 8                   # 8 / 16 / 32 (64 for doubles)
    signed: bool = False
    float: bool = False
    endian: str = ""                # "<" little, ">" big, "" not stated / 8-bit
    factor: float = 1.0             # physical = raw * factor + offset
    offset: float = 0.0
    equation: str = ""              # a non-linear conversion in X, if any
    units: str = ""
    #: a verbal conversion (A2L ``COMPU_VTAB``): raw value -> its text.
    #: Value files (DCM, CDFX) write such values as their texts.
    texts: Optional[Dict[int, str]] = None

    @property
    def size(self) -> int:
        return max(1, self.bits // 8)

    def equation_text(self) -> str:
        """The conversion as an equation in ``X`` (raw -> physical)."""
        if self.equation:
            return self.equation
        return linear_equation(self.factor, self.offset)


@dataclass
class AxisDef:
    count: int
    values: Optional[Storage] = None        # stored in the binary
    fixed: Optional[List[float]] = None     # given by the definition itself
    labels: Optional[List[str]] = None      # a text per point (enumerations)
    name: str = ""                          # what the axis is (description)
    #: the shared axis object this axis is (A2L ``AXIS_PTS_REF`` /
    #: ``CURVE_AXIS_REF``); value files address shared axes by this name
    ref: str = ""


@dataclass
class TableDef:
    name: str
    kind: str                               # constant / curve / map
    z: Storage
    rows: int = 1
    cols: int = 1
    description: str = ""
    categories: List[str] = field(default_factory=list)
    order: str = "row"                      # "row": X varies fastest
    x: Optional[AxisDef] = None
    y: Optional[AxisDef] = None
    decimals: Optional[int] = None
    note: str = ""
    #: "ascii" when the stored bytes are characters (a part number)
    text_kind: str = ""
    #: for a table whose sizes are stored in the image itself (an A2L layout
    #: with NO_AXIS_PTS): ``fn(read) -> TableDef`` gives the table as the
    #: image lays it out, where ``read(addr, size, signed, endian)`` reads
    #: an integer at a definition address (None outside the image)
    dynamic: Optional[object] = field(default=None, repr=False, compare=False)
    #: definition address where the table's record starts (its stored point
    #: counts or header, ahead of the axes) when the format says so (A2L,
    #: DAMOS); None: the first stored element
    record: Optional[int] = None
    #: the identifier the source format names the table by (an A2L / DAMOS
    #: symbol), kept apart from ``name`` so translating display text never
    #: touches it; "" when the format has none beyond ``name``
    symbol: str = ""
    #: source-language text of each field a translation rewrote
    #: (``{"name": ..., "description": ...}``)
    originals: Dict[str, str] = field(default_factory=dict)
    #: what a writer could not express for this table (filled when written)
    loss: List[str] = field(default_factory=list)


@dataclass
class Definition:
    type: str                               # xdf / a2l / ols / kp / dam
    title: str = ""
    tables: List[TableDef] = field(default_factory=list)
    base: int = 0
    #: address ranges the definition says it covers (ECU addresses),
    #: ``[(start, size)]`` -- an A2L's memory segments
    segments: List[tuple] = field(default_factory=list)
    info: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    #: ``fn(image size) -> base`` when the base depends on the image (an
    #: A2L's memory segments, a DAMOS program base address)
    base_for: Optional[object] = field(default=None, repr=False, compare=False)
    #: ``fn(image bytes) -> base or None`` when the definition can find its
    #: own place in an image from what the image structurally holds (a
    #: DAMOS: every map header starts with its input's RAM address)
    locate: Optional[object] = field(default=None, repr=False, compare=False)
    #: said when :meth:`base_by_evidence` chose the base over the guess
    base_note: str = field(default="", repr=False, compare=False)
    #: the image the definition was made for, when the file carries it (a
    #: WinOLS project): ``(bytes, base)``, definition address - base = offset
    reference: Optional[tuple] = field(default=None, repr=False, compare=False)
    #: checksum blocks the definition carries (an XDF's XDFCHECKSUM entries,
    #: as dicts of their fields; ``"raw"`` holds the element as read) or that
    #: an export should write
    checksums: List[dict] = field(default_factory=list)
    #: TunerPro XDFPATCH blocks, byte-literal: ``{"title", "description",
    #: "categories": [...], "entries": [{"name", "address" (a definition
    #: address), "patch": bytes, "base": bytes or None}]}``
    patches: List[dict] = field(default_factory=list)
    #: every image the file embeds, ``[(label, bytes, base)]`` (a WinOLS
    #: project's original and its stored versions)
    images: List[tuple] = field(default_factory=list, repr=False, compare=False)
    #: format-specific records kept verbatim so a same-format write can
    #: re-emit what it did not change (a DAMOS file's raw records)
    passthrough: Optional[object] = field(default=None, repr=False, compare=False)

    def base_in(self, data) -> int:
        """The base for this image: found in it when the definition can,
        else from its size."""
        if self.locate is not None:
            try:
                b = self.locate(data)
            except Exception:
                b = None
            if b is not None:
                return int(b)
        return self.base_by_evidence(len(data))

    def tables_inside(self, base: int, size: int) -> int:
        """How many tables have their stored values inside an image of
        ``size`` bytes whose offset 0 is definition address ``base``."""
        n = 0
        for t in self.tables:
            a = t.z.addr
            if a is not None and 0 <= a - base < size:
                n += 1
        return n

    def base_by_evidence(self, size: int) -> int:
        """The base for an image of ``size`` bytes when nothing in it says:
        the one the definition's memory segments give (:meth:`base_of`),
        unless that leaves most of the tables outside the image while the
        definition's own base (addresses are image offsets) puts them in --
        a guess about where an image sits (an A2L / DAMOS stating no
        segments: "a 512 KB image at 0x80000") must not throw away tables
        the addresses place inside it."""
        guess = self.base_of(size)
        total = sum(1 for t in self.tables if t.z.addr is not None)
        if not total or guess == self.base:
            return guess
        have = self.tables_inside(guess, size)
        if have >= 0.9 * total:
            return guess
        alt = self.tables_inside(self.base, size)
        if alt > have:
            self.base_note = (
                "Offset 0 of the image is definition address 0x%X: the "
                "definition does not say where an image of 0x%X bytes sits, "
                "and the usual guess (0x%X) would leave %d of its %d tables "
                "outside it, against %d inside at 0x%X. Give the base to "
                "override." % (self.base, size, guess, total - have, total,
                               alt, self.base))
            return self.base
        return guess

    def base_of(self, size: int) -> int:
        if self.base_for is not None:
            try:
                return int(self.base_for(size))
            except Exception:
                return self.base
        return self.base

    def byte_order(self) -> str:
        """The byte order most multi-byte values are stored in (a session
        reads every value one way), ``">"`` when nothing says."""
        votes = {"<": 0, ">": 0}
        for t in self.tables:
            for st in (t.z, t.x.values if t.x else None, t.y.values if t.y else None):
                if st is not None and st.bits > 8 and st.endian in votes:
                    votes[st.endian] += 1
        return "<" if votes["<"] > votes[">"] else ">"


def _num(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))


def linear_equation(factor: float, offset: float) -> str:
    """``X*factor+offset`` in the equation syntax the editors read."""
    if not (math.isfinite(factor) and math.isfinite(offset)):
        return "X"
    if factor == 0:
        factor = 1.0
    eq = "X" if factor == 1 else "X*%s" % _num(factor)
    if offset:
        eq += ("+%s" % _num(offset)) if offset > 0 else ("-%s" % _num(-offset))
    return eq

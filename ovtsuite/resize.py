# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Resizing a map or curve: changing how many breakpoints its axes have.

Bosch EDC15/EDC16 tables are self-describing records -- count words, then the
breakpoints, then the cells -- and the ECU reads the counts from the record
on every call, so a table can hold fewer points than the space its
definition reserves for it (the A2L/DAMOS maximum dimensions). This module
rewrites a record to new dimensions inside that space ("in place"). Moving a
record elsewhere and re-pointing the ECU at it ("relocate") is designed in
docs/MAP_RESIZE.md; :data:`RELOCATE_READY` says whether this build has it.

What a record looks like is platform knowledge, so it lives in the platform's
module behind the ``layout`` hook (:class:`LayoutCodec`). Everything here is
generic: the limits, the resampling, the undoable write and the *layout
modifiers*.

Layout modifiers
    A project keeps, per set, the resizes made to it (:func:`dumps` /
    :func:`loads`, ``Project.layout_mods``). They are kept OUTSIDE the stored
    definition, like user parameters, and :func:`apply` runs on every open
    after the module has built its model -- so a module update that rebuilds
    the definition from the stock knowledge base cannot put a table back to
    its stock size. The bytes stay the authority: a modifier re-shapes a table
    only to a size its record in the image actually holds, so a stock file, or
    a version saved before the resize, keeps the stock layout. A modifier is
    never dropped (undo, or resizing back to stock, keeps it): it lists every
    size the table has had, so any version of the set reads correctly.

No Qt here.
"""

from __future__ import annotations

import copy
import json
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

#: format of the stored modifier list
FORMAT = 1
#: largest axis the structural placers recognise (ovtsuite.structure.MAX_AXIS)
MAX_POINTS = 64
#: relocate (move a record to free space and re-point the ECU at it) exists;
#: whether it is available for a given table is the codec's call
#: (:meth:`LayoutCodec.relocate_why`), since EDC15 has it and EDC16 does not
#: yet. ``RELOCATE_WHY`` is the fallback reason when no codec is loaded.
RELOCATE_READY = True
RELOCATE_WHY = ("Moving a table to free space isn't available for this ECU "
                "yet; for now a table can grow into the space its definition "
                "reserves for it.")


# ------------------------------------------------------------------ codec
@dataclass
class Shape:
    """A table record as the image holds it."""
    start: int                     # first byte of the record (its header)
    nx: int                        # points on the X axis (cols)
    ny: int                        # points on the Y axis (rows); 1 for a curve
    x_addr: int                    # X breakpoints (a curve's only axis)
    y_addr: Optional[int]          # Y breakpoints (maps)
    z_addr: int                    # first cell
    end: int                       # one past the last cell


class LayoutCodec:
    """What a platform module provides through its ``layout`` hook.

    ``start_of(e)``   -> where ``e``'s record begins, worked out from the
                         entity alone (its value address and dimensions), or
                         None when its layout is not one this codec rewrites
                         (``why(e)`` then says why). Not verified.
    ``read(data, start, e)`` -> the :class:`Shape` stored at ``start``, or
                         None when no valid record of ``e``'s kind is there.
    ``encode(data, start, e, nx, ny, xs, ys, zs)`` -> the record's new bytes
                         (``xs``/``ys`` raw breakpoints, ``zs`` raw cells in
                         storage order).
    ``footprint(e, nx, ny)`` -> how many bytes a record of that size takes.
    ``bounds(session)`` -> extra format-defined starts of other objects (for
                         EDC15: every target of the dataset's own pointer
                         table), on top of the parameters the session places.
    """

    def start_of(self, e) -> Optional[int]:
        raise NotImplementedError

    def why(self, e) -> str:
        return "this table's layout cannot be rewritten"

    def read(self, data, start: int, e) -> Optional[Shape]:
        raise NotImplementedError

    def encode(self, data, start: int, e, nx: int, ny: int,
               xs, ys, zs) -> bytes:
        raise NotImplementedError

    def footprint(self, e, nx: int, ny: int) -> int:
        raise NotImplementedError

    def bounds(self, session) -> List[int]:
        return []

    # derived -----------------------------------------------------------
    def record(self, data, e) -> Optional[Shape]:
        """``e``'s record, verified: present, and holding ``e``'s size."""
        start = self.start_of(e)
        if start is None:
            return None
        shp = self.read(data, start, e)
        if shp is None or (shp.nx, shp.ny) != dims(e) or shp.z_addr != e.addr:
            return None
        return shp

    # -- relocation (optional; the base platform does not support it) ----
    # Moving a record to free space and re-pointing the ECU at it, so a table
    # can grow past the room reserved for it. A platform supports it only when
    # the ECU reaches the table through a pointer the tool can rewrite (EDC15's
    # struct-pointer slots); EDC16 addresses tables as block base + fixed
    # displacement, which would mean patching code, so it stays unsupported.
    def relocate_why(self, session, e) -> Optional[str]:
        """None when ``e`` can be relocated, else why not."""
        return ("moving a table to free space isn't available for this ECU "
                "yet")

    def container_of(self, session, e) -> Optional[Tuple[int, int]]:
        """``(base, end)`` of the region ``e`` lives in and may be moved
        within (an EDC15 dataset), or None."""
        return None

    def slots_for(self, data, e, start: int) -> List[int]:
        """Absolute addresses of the pointer words the ECU reaches ``e``'s
        record (at ``start``) through -- all of them, so every one is
        re-pointed together. Empty when none is found."""
        return []

    def free_spans(self, session, e) -> List[Tuple[int, int]]:
        """``(offset, length)`` runs of free space ``e`` may be moved into,
        within its container, never touching a live object or the checksum
        words. Sorted, lowest first."""
        return []

    def encode_pointer(self, data, slot_addr: int, new_start: int,
                       base: int) -> List[Tuple[int, int, int]]:
        """Byte writes that re-point the slot at ``slot_addr`` to the record
        at ``new_start`` -- ``[(addr, old, new), ...]``."""
        return []

    def slot_target(self, data, slot_addr: int, base: int) -> Optional[int]:
        """Where the slot at ``slot_addr`` currently points (a file offset),
        or None when it points outside the container. For a pointer-per-table
        platform (EDC15) this is the record start; for a pointer-per-block
        platform (EDC16) it is the block base, and the record sits at
        ``slot_target + disp`` (the modifier carries ``disp``)."""
        return None

    def relocate(self, session, e, new_x, new_y,
                 used: List[Tuple[int, int]]) -> Optional["Plan"]:
        """A platform-specific relocation plan for ``e``, or None to use the
        generic pointer-per-table path (:func:`relocate_one`). EDC16 moves a
        whole block, so it supplies its own; EDC15 returns None."""
        return None


def codec_for(session) -> Optional[LayoutCodec]:
    """The installed module's layout codec for ``session``'s platform."""
    from .modules import registry as _registry
    m = _registry.for_platform(getattr(session, "platform", "") or "")
    if m is None or not getattr(m, "layout", None) or \
            getattr(m, "variant", "full") != "full":
        return None
    try:
        return m.call("layout")
    except Exception:
        return None


# ------------------------------------------------------------------ helpers
def curve_axis(e):
    return e.x if e.x is not None else e.y


def dims(e) -> Tuple[int, int]:
    """``(nx, ny)`` of an entity: X points, Y points (1 for a curve)."""
    if e.kind == "map":
        return e.cols, e.rows
    return max(e.rows, e.cols), 1


def fmt_dims(e, nx: int, ny: int) -> str:
    return ("%d x %d" % (nx, ny)) if e.kind == "map" else "%d points" % nx


def _mono(v) -> bool:
    if len(v) < 2:
        return True
    return all(a < b for a, b in zip(v, v[1:])) or \
        all(a > b for a, b in zip(v, v[1:]))


def _read_axis(bf, addr: int, n: int, ax) -> List[float]:
    """Raw breakpoints at ``addr``, signed the way ``ax`` reads them."""
    bits = ax.bits if ax is not None else 16
    if ax is not None and ax.is_float:
        return [struct.unpack_from(bf.endian + "f", bf.data, addr + 4 * i)[0]
                for i in range(n)]
    if ax is not None and ax.signed is not None:
        return list(bf.read_many(addr, n, bits, ax.signed))
    un = bf.read_many(addr, n, bits, False)
    if _mono(un):
        return list(un)
    return list(bf.read_many(addr, n, bits, True))


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    """Linear interpolation, held at the ends -- what the ECU does."""
    n = len(xs)
    if n == 0:
        return 0.0
    if n == 1:
        return float(ys[0])
    inc = xs[-1] >= xs[0]
    if (x <= xs[0]) if inc else (x >= xs[0]):
        return float(ys[0])
    if (x >= xs[-1]) if inc else (x <= xs[-1]):
        return float(ys[-1])
    for i in range(n - 1):
        a, b = xs[i], xs[i + 1]
        if (a <= x <= b) if inc else (b <= x <= a):
            f = 0.0 if b == a else (x - a) / (b - a)
            return ys[i] + f * (ys[i + 1] - ys[i])
    return float(ys[-1])


def resample(old_x, old_y, old_z, new_x, new_y) -> List[List[float]]:
    """``old_z`` (``[row][col]``, rows on ``old_y``, cols on ``old_x``)
    evaluated on ``new_x``/``new_y``: bilinear, held at the edges -- exactly
    what the ECU interpolates there, so resizing alone changes nothing the
    engine does inside the old axis range."""
    rows = len(old_z)
    out = []
    for yv in (list(new_y) or [0.0]):
        if rows == 1:
            line = [float(v) for v in old_z[0]]
        else:
            line = [_interp(old_y, [old_z[r][c] for r in range(rows)], yv)
                    for c in range(len(old_z[0]))]
        out.append([_interp(old_x, line, xv) for xv in new_x])
    return out


def spread(old: Sequence[float], n: int, how: str = "extend") -> List[float]:
    """Proposed breakpoints when an axis gets ``n`` points (display units).

    ``"extend"`` keeps the existing points and adds new ones past the last
    with the last step (or drops points from the end when shrinking);
    ``"even"`` spreads ``n`` points evenly over the current first..last.
    """
    old = [float(v) for v in old]
    if not old:
        return [float(i) for i in range(n)]
    if how == "even" or len(old) < 2:
        a, b = old[0], old[-1]
        if n < 2:
            return [a]
        return [a + (b - a) * i / (n - 1) for i in range(n)]
    if n <= len(old):
        return old[:n]
    step = old[-1] - old[-2]
    out = list(old)
    while len(out) < n:
        out.append(out[-1] + step)
    return out


# ------------------------------------------------------------------ copies
def copies_of(session, e) -> List[object]:
    """The other copies of the same table that must change with it: its
    coding-variant copies (EDC16 secondary block) and the same table in the
    other datasets (EDC15). One table, one shape -- the ECU picks a copy by
    coding, and copies left at another size would be a different
    calibration per coding."""
    out = []
    if getattr(e, "source", "") == "variant":
        return out                      # resized from its main copy
    for o in getattr(session, "entities", []) or []:
        if o is not e and o.name == e.name and o.kind == e.kind and \
                (o.group or "") != (e.group or ""):
            out.append(o)
    try:
        variants = list(session.variant_entities())
    except Exception:
        variants = []
    pre = e.name + " (variant @"
    out += [v for v in variants if v.name.startswith(pre) and v.kind == e.kind]
    return out


def main_of(session, e):
    """The main copy a coding-variant copy is resized from (else ``e``)."""
    if getattr(e, "source", "") != "variant":
        return e
    base = e.name.split(" (variant @")[0]
    for o in getattr(session, "entities", []) or []:
        if o.name == base and o.kind == e.kind:
            return o
    return e


# ------------------------------------------------------------------ limits
@dataclass
class Limits:
    """How far one copy of a table can be resized in place."""
    entity: object
    start: int
    nx: int                    # current
    ny: int
    max_nx: int                # largest that fits with the other axis as is
    max_ny: int
    room: int                  # bytes available from ``start``
    next_at: Optional[int]     # where the next object begins (None: none known)
    reason: str = ""           # non-empty: not resizable
    decl_nx: int = 0           # the definition's (reserved) size per axis
    decl_ny: int = 0

    def fits(self, codec, nx: int, ny: int) -> Optional[str]:
        """None when ``nx`` x ``ny`` fits here, else why not."""
        e = self.entity
        if self.reason:
            return self.reason
        if nx < 2 or (e.kind == "map" and ny < 2):
            return "an axis needs at least 2 points"
        if nx > MAX_POINTS or ny > MAX_POINTS:
            return "an axis can have at most %d points" % MAX_POINTS
        if nx > max(self.nx, self.decl_nx) or ny > max(self.ny, self.decl_ny):
            return ("the definition reserves room for at most %s"
                    % fmt_dims(e, max(self.nx, self.decl_nx),
                               max(self.ny, self.decl_ny)))
        need = codec.footprint(e, nx, ny if e.kind == "map" else 1)
        if need > self.room:
            return ("%s needs %d bytes; %d are free before the next object "
                    "at 0x%X" % (fmt_dims(e, nx, ny), need, self.room,
                                 self.next_at or 0)
                    if self.next_at is not None else
                    "%s needs %d bytes; the definition reserves %d"
                    % (fmt_dims(e, nx, ny), need, self.room))
        return None


def _starts(session, codec, skip) -> List[int]:
    """Where every other object's bytes begin (sorted)."""
    out = list(codec.bounds(session) or ())
    ents = list(getattr(session, "entities", []) or [])
    try:
        ents += list(session.variant_entities())
    except Exception:
        pass
    skip_ids = {id(x) for x in skip}
    set_start = getattr(codec, "set_start", None)
    for o in ents:
        if id(o) in skip_ids:
            continue
        s = None
        if o.kind in ("map", "curve"):
            try:
                s = codec.start_of(o)
                if s is None and set_start is not None:
                    s = set_start(o)
                    if s is not None:       # a set member: its cells too
                        out.append(o.addr)
            except Exception:
                s = None
        if s is None:
            cand = [o.addr]
            if getattr(o, "source", "") != "variant":
                cand += [a.addr for a in (o.x, o.y)
                         if a is not None and a.addr is not None]
            s = min(cand)
        out.append(s)
    return sorted(set(out))


def limits(session, e, codec: Optional[LayoutCodec] = None,
           starts: Optional[List[int]] = None) -> Limits:
    """What resizing this one copy ``e`` in place allows.

    The ceiling is format-defined, never guessed: the record may take the
    space its definition declares for it (the A2L / DAMOS maximum size --
    the room Bosch reserved), and must still end before the next object the
    image is known to hold.
    """
    codec = codec or codec_for(session)
    nx, ny = dims(e)

    def no(why):
        return Limits(e, 0, nx, ny, nx, ny, 0, None, why)

    if codec is None:
        return no("the installed module cannot resize tables on this platform")
    if e.kind not in ("map", "curve"):
        return no("only maps and curves have axes to resize")
    if set_of(session, e, codec):
        return set_limits(session, e, codec)
    start = codec.start_of(e)
    if start is None:
        return no(codec.why(e))
    shp = codec.read(e.binfile.data, start, e)
    if shp is None or (shp.nx, shp.ny) != (nx, ny) or shp.z_addr != e.addr:
        return no("the record in the image does not match this table "
                  "(0x%X)" % start)
    if e.kind == "map":
        dnx, dny = e.declared_cols or nx, e.declared_rows or ny
    else:
        dnx, dny = max(e.declared_rows or 1, e.declared_cols or 1), 1
    declared = codec.footprint(e, max(nx, dnx), max(ny, dny))
    if starts is None:
        starts = _starts(session, codec, [e])
    nxt = next((s for s in starts if s >= shp.end), None)
    room = declared if nxt is None else min(declared, nxt - start)
    room = max(room, shp.end - start)       # never less than it uses now
    # each axis stays within the size the definition reserves for it (the
    # format's own limit), and the record within the room
    cap_x, cap_y = min(MAX_POINTS, max(nx, dnx)), min(MAX_POINTS, max(ny, dny))
    max_nx, max_ny = nx, ny
    while max_nx < cap_x and codec.footprint(e, max_nx + 1, ny) <= room:
        max_nx += 1
    if e.kind == "map":
        while max_ny < cap_y and codec.footprint(e, nx, max_ny + 1) <= room:
            max_ny += 1
    return Limits(e, start, nx, ny, max_nx, max_ny, room, nxt,
                  decl_nx=dnx, decl_ny=dny)


def all_limits(session, e, codec=None) -> List[Limits]:
    """Limits of ``e`` and every copy that changes with it."""
    codec = codec or codec_for(session)
    group = [e] + copies_of(session, e)
    if codec is None:
        return [limits(session, x, None) for x in group]
    if set_of(session, e, codec):
        return [limits(session, x, codec) for x in group]
    starts = _starts(session, codec, group)
    return [limits(session, x, codec, starts) for x in group]


# ------------------------------------------------------------------ plan
@dataclass
class Plan:
    """A resize of one copy, worked out but not written."""
    entity: object
    start: int                     # where the entity ends up pointing
    old: Tuple[int, int]
    new: Tuple[int, int]
    data: bytes                    # inplace: the record's new bytes (old span)
    old_bytes: bytes               # inplace: what they replace
    shape: Shape                   # the record after writing
    mode: str = "inplace"          # 'inplace' | 'relocate'
    # relocate: the record goes to free space and the old one is left as a
    # fallback, so the write is an explicit byte diff rather than an overlay.
    writes: Optional[List[Tuple[int, int, int]]] = None   # (addr, old, new)
    new_start: int = 0             # where the record ends up
    old_start: int = 0
    base: int = 0                  # container base, for the slot pointers
    slots: Tuple[int, ...] = ()    # slot addresses re-pointed
    disp: int = 0                  # record start - slot target (EDC16 block)
    reserve: int = 0               # bytes the move claims in free space
    coverage: Optional[dict] = None  # EDC16: how the table's refs were carried
    # a map set (one plan per member, and per axis shown as a curve of its
    # own): the copy's first plan carries every write and ``set_info``
    set_info: Optional[dict] = None
    view: str = ""                 # 'x' / 'y': a curve showing that set axis

    @property
    def noop(self) -> bool:
        if self.mode == "relocate":
            return False
        if self.writes is not None:
            return self.new == self.old and not self.writes
        return self.new == self.old and self.data == self.old_bytes

    def byte_writes(self) -> List[Tuple[int, int, int]]:
        """The (addr, old, new) writes this plan makes."""
        if self.writes is not None:
            return list(self.writes)
        out = []
        for i, (o, nb) in enumerate(zip(self.old_bytes, self.data)):
            if o != nb:
                out.append((self.start + i, o, nb))
        return out


def _encode_axis(ax, values) -> List:
    if ax is None:
        return []
    out = []
    for v in values:
        r = ax.eq.encode(float(v))
        out.append(float(r) if ax.is_float else int(round(r)))
    return out


def _check_axis(name, raw, ax, old_raw) -> None:
    if ax is None or not raw:
        return
    if len(set(raw)) != len(raw) or not _mono(raw):
        raise ValueError("the %s breakpoints must keep strictly rising (or "
                         "falling) once rounded to what the ECU stores" % name)
    if len(old_raw) >= 2 and len(raw) >= 2 and \
            (old_raw[-1] > old_raw[0]) != (raw[-1] > raw[0]):
        raise ValueError("the %s axis must keep its direction" % name)
    if ax.is_float:
        return
    signed = bool(ax.signed) or min(old_raw or [0]) < 0 or min(raw) < 0
    lo = -(1 << (ax.bits - 1)) if signed else 0
    hi = (1 << (ax.bits - 1)) - 1 if signed else (1 << ax.bits) - 1
    if min(raw) < lo or max(raw) > hi:
        raise ValueError("a %s breakpoint is outside what the ECU can store "
                         "(%d..%d raw)" % (name, lo, hi))


def _encode_resized(e, codec, data, enc_start: int, old: Shape,
                    new_x, new_y) -> Tuple[bytes, int, int]:
    """The record ``e`` becomes at ``new_x``/``new_y`` (display units),
    resampled from its current cells -- the bytes alone, for either mode.
    ``enc_start`` is where the OLD record sits (its RAM pointers carry over).
    Raises ValueError on an axis that will not store."""
    xs, ys, flat, nx, ny = _resampled(e, old, new_x, new_y)
    return codec.encode(data, enc_start, e, nx, ny, xs, ys, flat), nx, ny


def _resampled(e, old: Shape, new_x, new_y):
    """``(xs, ys, cells, nx, ny)``: ``e`` on ``new_x``/``new_y`` (display
    units) as raw breakpoints and raw cells in storage order, resampled from
    its current cells. Raises ValueError on an axis that will not store."""
    bf = e.binfile
    nx = len(new_x)
    ny = len(new_y) if e.kind == "map" else 1
    if e.kind == "map":
        xa, ya = e.x, e.y
    else:
        xa, ya = curve_axis(e), None
    ox = _read_axis(bf, old.x_addr, old.nx, xa)
    oy = _read_axis(bf, old.y_addr, old.ny, ya) if e.kind == "map" else []
    xs = _encode_axis(xa, new_x)
    ys = _encode_axis(ya, new_y) if e.kind == "map" else []
    _check_axis("X", xs, xa, ox)
    _check_axis("Y", ys, ya, oy)
    if e.kind == "map":
        old_z = [[e.raw_cell(r, c) for c in range(e.cols)]
                 for r in range(e.rows)]
        zr = resample([float(v) for v in ox], [float(v) for v in oy], old_z,
                      [float(v) for v in xs], [float(v) for v in ys])
    else:
        line = [e.raw_cell(r, c) for r in range(e.rows) for c in range(e.cols)]
        zr = [[_interp([float(v) for v in ox], line, float(v)) for v in xs]]
    lo, hi = e.raw_range
    z = [[(float(v) if e.is_float else max(lo, min(hi, int(round(v)))))
          for v in row] for row in zr]
    if e.kind == "map":
        if e.order == "col":
            flat = [z[r][c] for c in range(nx) for r in range(ny)]
        else:
            flat = [z[r][c] for r in range(ny) for c in range(nx)]
    else:
        flat = z[0]
    return xs, ys, flat, nx, ny


def plan_one(session, e, new_x: Sequence[float], new_y: Sequence[float] = (),
             codec: Optional[LayoutCodec] = None,
             lim: Optional[Limits] = None) -> Plan:
    """Work out copy ``e`` resized IN PLACE to ``new_x``/``new_y`` (display
    units). The cells are the table's own, resampled onto the new axes
    (:func:`resample`). Raises ValueError with a plain reason when it does
    not fit."""
    codec = codec or codec_for(session)
    if codec is None:
        raise ValueError("the installed module cannot resize this table")
    lim = lim or limits(session, e, codec)
    nx = len(new_x)
    ny = len(new_y) if e.kind == "map" else 1
    why = lim.fits(codec, nx, ny)
    if why:
        raise ValueError("%s: %s" % (e.title, why))
    data = bytes(e.binfile.data)
    old = codec.read(data, lim.start, e)
    new, nx, ny = _encode_resized(e, codec, data, lim.start, old, new_x, new_y)
    span = max(len(new), old.end - lim.start)
    old_bytes = data[lim.start:lim.start + span]
    new_full = bytes(new) + old_bytes[len(new):]   # a freed tail stays as it was
    after = bytearray(data)
    after[lim.start:lim.start + span] = new_full
    shp = codec.read(bytes(after), lim.start, e)
    if shp is None or (shp.nx, shp.ny) != (nx, ny):
        raise ValueError("%s: the rewritten record does not read back "
                         "(nothing was written)" % e.title)
    return Plan(e, lim.start, (old.nx, old.ny), (nx, ny), new_full,
                old_bytes, shp)


def _even(n: int) -> int:
    return n + (n & 1)


def _alloc(codec, session, e, need: int, used: List[Tuple[int, int]]
           ) -> Optional[int]:
    """A free offset for a ``need``-byte record in ``e``'s container, not
    overlapping ``used`` (prior relocations this batch and in the project).
    None when nothing fits."""
    return _alloc_in(codec.free_spans(session, e), need, used)


def _alloc_in(spans, need: int, used: List[Tuple[int, int]]) -> Optional[int]:
    """:func:`_alloc` over free ``spans`` already worked out."""
    for fs, fl in spans:
        pos = fs + (fs & 1)
        blocks = sorted(u for u in used if u[0] < fs + fl and u[1] > fs)
        for us, ue in blocks:
            if us - pos >= need:
                return pos
            pos = max(pos, _even(ue))
        if fs + fl - pos >= need:
            return pos
    return None


def _used_spans(session, codec) -> List[Tuple[int, int]]:
    """Free-space already taken by relocations kept in the session (so a
    second relocation in the same dataset does not land on the first)."""
    out = []
    for m in mods(session):
        for r in m.get("relocate") or []:
            ns = r.get("new_base", r.get("new_start"))
            fp = r.get("bytes")
            if ns and fp:
                out.append((int(ns), int(ns) + int(fp)))
        for a, b in m.get("reserved") or []:       # a moved map set's pieces
            out.append((int(a), int(b)))
    return out


def relocate_one(session, e, new_x, new_y, codec, used: List[Tuple[int, int]]
                 ) -> Plan:
    """Work out copy ``e`` MOVED to free space at ``new_x``/``new_y``. The
    old record is left in place as a fallback; the ECU's pointer slots are
    re-pointed at the new one. Raises ValueError with a plain reason."""
    nx = len(new_x)
    ny = len(new_y) if e.kind == "map" else 1
    if nx < 2 or (e.kind == "map" and ny < 2):
        raise ValueError("%s: an axis needs at least 2 points" % e.title)
    if nx > MAX_POINTS or ny > MAX_POINTS:
        raise ValueError("%s: an axis can have at most %d points"
                         % (e.title, MAX_POINTS))
    data = bytes(e.binfile.data)
    old_start = codec.start_of(e)
    if old_start is None:
        raise ValueError("%s: %s" % (e.title, codec.why(e)))
    old = codec.read(data, old_start, e)
    if old is None or (old.nx, old.ny) != dims(e):
        raise ValueError("%s: its record in the image does not match the "
                         "table" % e.title)
    cont = codec.container_of(session, e)
    if cont is None:
        raise ValueError("%s: %s" % (e.title, codec.relocate_why(session, e)))
    base, _end = cont
    slots = codec.slots_for(data, e, old_start)
    if not slots:
        raise ValueError("%s: the pointer the ECU uses to reach this table "
                         "could not be found, so it can't be moved safely"
                         % e.title)
    new, nx, ny = _encode_resized(e, codec, data, old_start, old, new_x, new_y)
    need = _even(len(new))
    new_start = _alloc(codec, session, e, need, used)
    if new_start is None:
        raise ValueError("%s: not enough free space in the dataset to move it "
                         "there (needs %d bytes)" % (e.title, need))
    after = bytearray(data)
    after[new_start:new_start + len(new)] = new
    writes = [(new_start + i, data[new_start + i], b) for i, b in enumerate(new)
              if data[new_start + i] != b]
    for s in slots:
        for a, o, nb in codec.encode_pointer(data, s, new_start, base):
            after[a] = nb
            if o != nb:
                writes.append((a, o, nb))
    shp = codec.read(bytes(after), new_start, e)
    if shp is None or (shp.nx, shp.ny) != (nx, ny) \
            or codec.slot_target(bytes(after), slots[0], base) != new_start:
        raise ValueError("%s: the moved record does not read back through its "
                         "slot (nothing was written)" % e.title)
    used.append((new_start, new_start + need))
    p = Plan(e, new_start, (old.nx, old.ny), (nx, ny), b"", b"", shp,
             mode="relocate", writes=writes, new_start=new_start,
             old_start=old_start, base=base, slots=tuple(slots))
    return p


def plan(session, e, new_x, new_y=(), codec=None, mode="inplace") -> List[Plan]:
    """Plans for ``e`` and every copy that changes with it -- all or none:
    a copy that cannot take the new size (or be moved) stops the whole
    resize. ``mode`` is ``"inplace"`` or ``"relocate"``."""
    codec = codec or codec_for(session)
    e = main_of(session, e)
    if codec is not None and set_of(session, e, codec):
        return plan_set(session, e, new_x, new_y, codec, mode)
    if mode == "relocate":
        if codec is None:
            raise ValueError("the installed module cannot resize this table")
        group = [e] + copies_of(session, e)
        used = _used_spans(session, codec)
        out = []
        for o in group:
            p = codec.relocate(session, o, new_x, new_y, used)
            out.append(p if p is not None
                       else relocate_one(session, o, new_x, new_y, codec, used))
        return out
    lims = all_limits(session, e, codec)
    return [plan_one(session, l.entity, new_x, new_y, codec, l) for l in lims]


# ------------------------------------------------------------------ map sets
# A map set ("Kennraum": EDC15 mrwBRA0..2KR, fnwSWGK0..9KR) is several maps
# on ONE pair of axis records, each member's cells stored apart. The ECU
# reads the axes once for the whole set, so the set changes size only all
# together: the axis records are rewritten once and every member's cells are
# resampled onto them, and the platform's pointers to each piece are
# re-pointed where a piece moves (codec hooks ``set_start``, ``read_axes``,
# ``encode_axes``, ``encode_cells``, ``axes_footprint``, ``set_pointers``).
# The modifier (``mode: "set"``) records the pointer slots, so a version of
# the set reads at whatever size ITS pointers lead to.

def _axes_key(e) -> tuple:
    axes = (e.x, e.y) if e.kind == "map" else (curve_axis(e),)
    return tuple(a.addr if a is not None else None for a in axes)


def set_of(session, e, codec=None) -> List[object]:
    """The members of ``e``'s map set in ``e``'s dataset (``e`` included),
    by cell address: every table on the same axis records. Empty for a table
    with axes of its own whose cells follow them (an ordinary record), or a
    platform with no sets. A member whose cells happen to follow the shared
    axes (the first, once a set has been moved) is still a member -- resized
    alone it would rewrite the axes under every other member."""
    codec = codec or codec_for(session)
    fn = getattr(codec, "set_start", None) if codec is not None else None
    if fn is None or e.kind not in ("map", "curve"):
        return []
    try:
        hdr = fn(e)
    except Exception:
        hdr = None
    if hdr is None:
        return []
    key, grp, dm = _axes_key(e), e.group or "", dims(e)
    out = [o for o in getattr(session, "entities", []) or []
           if o is not e and o.kind == e.kind and (o.group or "") == grp
           and _axes_key(o) == key and dims(o) == dm and o.addr != e.addr
           and fn(o) == hdr]
    if not out and codec.start_of(e) is not None:
        return []                         # an ordinary record
    return sorted(out + [e], key=lambda o: o.addr)


def set_main(session, e, codec=None):
    """The member a set's resize and modifier are named after (the first
    by cell address in the main copy's dataset); ``e`` when not a set."""
    e = main_of(session, e)
    members = set_of(session, e, codec)
    return members[0] if members else e


def _set_views(session, members, shp: Shape) -> List[Tuple[object, str]]:
    """Curves showing one of the set's axes as their values
    (``mrwDZstzv``: cells = the breakpoints): ``[(entity, 'x'|'y')]``."""
    e = members[0]
    grp = e.group or ""
    ids = {id(m) for m in members}
    out = []
    for o in getattr(session, "entities", []) or []:
        if o.kind != "curve" or id(o) in ids or (o.group or "") != grp:
            continue
        ax = curve_axis(o)
        if ax is None or ax.addr != o.addr:
            continue
        if o.addr == shp.x_addr:
            out.append((o, "x"))
        elif shp.y_addr is not None and o.addr == shp.y_addr:
            out.append((o, "y"))
    return out


def _view_shape(shp: Shape, which: str) -> Shape:
    a, n = (shp.x_addr, shp.nx) if which == "x" else (shp.y_addr, shp.ny)
    return Shape(a - 4, n, 1, a, None, a, a + 2 * n)


@dataclass
class SetLimits(Limits):
    """How far a map set (``entity`` and every member) can be resized in
    place: the shared axes before the next object, and each member's cells
    before theirs."""
    members: List[object] = None
    axes_room: int = 0
    cells_room: List[int] = None
    shape: Optional[Shape] = None          # the shared axes as they are

    def fits(self, codec, nx: int, ny: int) -> Optional[str]:
        e = self.entity
        if self.reason:
            return self.reason
        if nx < 2 or (e.kind == "map" and ny < 2):
            return "an axis needs at least 2 points"
        if nx > MAX_POINTS or ny > MAX_POINTS:
            return "an axis can have at most %d points" % MAX_POINTS
        if nx > max(self.nx, self.decl_nx) or ny > max(self.ny, self.decl_ny):
            return ("the definition reserves room for at most %s"
                    % fmt_dims(e, max(self.nx, self.decl_nx),
                               max(self.ny, self.decl_ny)))
        ny = ny if e.kind == "map" else 1
        need = codec.axes_footprint(e, nx, ny)
        if need > self.axes_room:
            return ("the set's shared axes need %d bytes; %d are free before "
                    "the next object" % (need, self.axes_room))
        cells = (e.bits // 8) * nx * ny
        for m, room in zip(self.members, self.cells_room):
            if cells > room:
                return ("%s's values need %d bytes; %d are free before the "
                        "next object -- move the set to free space instead"
                        % (m.name, cells, room))
        return None


def set_limits(session, e, codec=None) -> SetLimits:
    """What resizing ``e``'s whole set in place allows (format-defined, as
    for one table: each piece up to the size the definition reserves, and
    before the next object the image is known to hold)."""
    codec = codec or codec_for(session)
    members = set_of(session, e, codec)
    nx, ny = dims(e)

    def no(why):
        return SetLimits(e, 0, nx, ny, nx, ny, 0, None, why, members=members,
                         cells_room=[0] * len(members))

    if not members:
        return no(codec.why(e) if codec is not None else
                  "the installed module cannot resize tables on this platform")
    data = bytes(e.binfile.data)
    hdr = codec.set_start(e)
    shp = codec.read_axes(data, hdr, e)
    if shp is None or (shp.nx, shp.ny) != (nx, ny) or \
            shp.x_addr != (e.x.addr if e.kind == "map" else curve_axis(e).addr):
        return no("the set's axes in the image do not match this table "
                  "(0x%X)" % hdr)
    try:
        codec.set_pointers(data, hdr, e, [m.addr for m in members])
    except ValueError as ex:
        return no(str(ex))
    views = [v for v, _w in _set_views(session, members, shp)]
    starts = sorted(set(_starts(session, codec, members + views)
                        + [m.addr for m in members]))
    if e.kind == "map":
        dnx, dny = e.declared_cols or nx, e.declared_rows or ny
    else:
        dnx, dny = max(e.declared_rows or 1, e.declared_cols or 1), 1
    dnx, dny = max(nx, dnx), max(ny, dny)
    step = e.bits // 8

    def room(at, used, declared):
        nxt = next((s for s in starts if s >= at + used), None)
        r = declared if nxt is None else min(declared, nxt - at)
        return max(r, used), nxt

    axes_room, nxt = room(hdr, shp.end - hdr, codec.axes_footprint(e, dnx, dny))
    cells_room = [room(m.addr, step * nx * ny, step * dnx * dny)[0]
                  for m in members]
    lim = SetLimits(e, hdr, nx, ny, nx, ny, axes_room, nxt, decl_nx=dnx,
                    decl_ny=dny, members=members, axes_room=axes_room,
                    cells_room=cells_room, shape=shp)
    cap_x, cap_y = min(MAX_POINTS, dnx), min(MAX_POINTS, dny)
    while lim.max_nx < cap_x and lim.fits(codec, lim.max_nx + 1, ny) is None:
        lim.max_nx += 1
    if e.kind == "map":
        while lim.max_ny < cap_y and lim.fits(codec, nx, lim.max_ny + 1) is None:
            lim.max_ny += 1
    return lim


def plan_set(session, e, new_x, new_y=(), codec=None, mode="inplace"
             ) -> List[Plan]:
    """Plans for ``e``'s whole map set in its dataset and in every other
    dataset holding it -- all or none. Per copy, the first plan (the set's
    first member) carries every byte write and ``set_info``; the others
    re-shape the remaining members and the axis curves."""
    codec = codec or codec_for(session)
    if codec is None:
        raise ValueError("the installed module cannot resize this table")
    e = set_main(session, e, codec)
    used = _used_spans(session, codec) if mode == "relocate" else []
    names, out = None, []
    for o in [e] + copies_of(session, e):
        members = set_of(session, o, codec)
        got = [m.name for m in members]
        if not members:
            raise ValueError("%s [%s]: %s" % (o.title, o.group or "",
                                              codec.why(o)))
        if names is None:
            names = got
        elif got != names:
            raise ValueError("%s [%s]: its set is not the same maps (%s) as "
                             "in %s" % (o.title, o.group or "",
                                        ", ".join(got), e.group or "the set"))
        out += _plan_set_copy(session, members, new_x, new_y, codec, mode,
                              used)
    return out


def _plan_set_copy(session, members, new_x, new_y, codec, mode,
                   used: List[Tuple[int, int]]) -> List[Plan]:
    e0 = members[0]
    nx = len(new_x)
    ny = len(new_y) if e0.kind == "map" else 1
    if nx < 2 or (e0.kind == "map" and ny < 2):
        raise ValueError("%s: an axis needs at least 2 points" % e0.title)
    if nx > MAX_POINTS or ny > MAX_POINTS:
        raise ValueError("%s: an axis can have at most %d points"
                         % (e0.title, MAX_POINTS))
    data = bytes(e0.binfile.data)
    hdr = codec.set_start(e0)
    old = codec.read_axes(data, hdr, e0)
    if old is None or (old.nx, old.ny) != dims(e0):
        raise ValueError("%s: the set's axes in the image do not match the "
                         "table" % e0.title)
    try:
        ptrs = codec.set_pointers(data, hdr, e0, [m.addr for m in members])
    except ValueError as ex:
        raise ValueError("%s: %s" % (e0.title, ex))
    base = ptrs["base"]
    cells = []
    for m in members:
        xs, ys, flat, nx, ny = _resampled(m, old, new_x, new_y)
        cells.append(codec.encode_cells(m, flat))
    axes = codec.encode_axes(data, hdr, e0, nx, ny, xs, ys)
    reserved = []
    if mode == "relocate":
        if codec.container_of(session, e0) is None:
            raise ValueError("%s: %s" % (e0.title,
                                         codec.relocate_why(session, e0)))
        spans = codec.free_spans(session, e0)
        mine = []
        for blob in [axes] + cells:
            at = _alloc_in(spans, _even(len(blob)), used + mine)
            if at is None:
                need = sum(_even(len(b)) for b in [axes] + cells)
                raise ValueError(
                    "%s: not enough free space in the dataset to move the set "
                    "there (needs %d bytes, in %d pieces)"
                    % (e0.title, need, 1 + len(cells)))
            mine.append((at, at + _even(len(blob))))
        reserved = mine
        used.extend(mine)                 # the next dataset's copy, and later
        new_hdr = mine[0][0]
        new_cells = [a for a, _b in mine[1:]]
    else:
        lim = set_limits(session, e0, codec)
        why = lim.fits(codec, nx, ny)
        if why:
            raise ValueError("%s: %s" % (e0.title, why))
        new_hdr, new_cells = hdr, [m.addr for m in members]
    after = bytearray(data)
    touched = set()

    def put(at, blob):
        after[at:at + len(blob)] = blob
        touched.update(range(at, at + len(blob)))

    put(new_hdr, axes)
    for at, blob in zip(new_cells, cells):
        put(at, blob)
    x_rec = new_hdr + 4 + 2 * ny
    targets = [(ptrs["axes"][0], new_hdr)]
    if e0.kind == "map":
        targets.append((ptrs["axes"][1], x_rec))
    targets += list(zip(ptrs["cells"], new_cells))
    for slots, to in targets:
        for s in slots:
            if codec.slot_target(data, s, base) == to:
                continue
            for a, _o, nb in codec.encode_pointer(data, s, to, base):
                after[a] = nb
                touched.add(a)
    writes = [(a, data[a], after[a]) for a in sorted(touched)
              if data[a] != after[a]]
    # read back through the pointers, as the ECU will
    blob = bytes(after)
    shp = codec.read_axes(blob, new_hdr, e0)
    ok = (shp is not None and (shp.nx, shp.ny) == (nx, ny)
          and all(codec.slot_target(blob, s, base) == to
                  for slots, to in targets for s in slots))
    if not ok:
        raise ValueError("%s: the rewritten set does not read back through its "
                         "pointers (nothing was written)" % e0.title)
    info = {"group": e0.group or "", "base": base,
            "axis_slots": [list(s) for s in ptrs["axes"]],
            "cell_slots": [list(s) for s in ptrs["cells"]],
            "old_hdr": hdr, "hdr": new_hdr, "cells": list(new_cells),
            "reserved": [list(r) for r in reserved]}
    out = []
    for i, (m, at) in enumerate(zip(members, new_cells)):
        mshape = Shape(new_hdr, nx, ny, shp.x_addr, shp.y_addr, at,
                       at + len(cells[i]))
        out.append(Plan(m, new_hdr, (old.nx, old.ny), (nx, ny), b"", b"",
                        mshape, mode=mode, writes=writes if i == 0 else [],
                        new_start=new_hdr, old_start=hdr, base=base,
                        slots=tuple(ptrs["axes"][0]),
                        set_info=info if i == 0 else None))
    for v, which in _set_views(session, members, old):
        out.append(Plan(v, new_hdr, (old.nx, old.ny), (nx, ny), b"", b"",
                        _view_shape(shp, which), mode=mode, writes=[],
                        new_start=new_hdr, old_start=hdr, base=base,
                        view=which))
    return out


# ------------------------------------------------------------------ entity
def _own_axes(e) -> None:
    """Give ``e`` axis objects of its own: entities made with
    ``dataclasses.replace`` (coding-variant copies, sibling sessions) share
    them, and re-pointing a shared axis would re-point the other entity."""
    if e.x is not None:
        e.x = copy.copy(e.x)
    if e.y is not None:
        e.y = copy.copy(e.y)


def layout_of(e) -> dict:
    """The entity fields a resize changes (for undo)."""
    return {"addr": e.addr, "rows": e.rows, "cols": e.cols,
            "x": (e.x.addr, e.x.count) if e.x is not None else None,
            "y": (e.y.addr, e.y.count) if e.y is not None else None,
            "note": e.note, "name": e.name}


def set_layout(e, lay: dict) -> None:
    _own_axes(e)
    e.addr, e.rows, e.cols = lay["addr"], lay["rows"], lay["cols"]
    if e.x is not None and lay["x"] is not None:
        e.x.addr, e.x.count = lay["x"]
    if e.y is not None and lay["y"] is not None:
        e.y.addr, e.y.count = lay["y"]
    e.note = lay["note"]
    e.name = lay.get("name", e.name)


def shape_entity(e, shp: Shape) -> None:
    """Point ``e`` at the record ``shp`` describes."""
    _own_axes(e)
    e.addr = shp.z_addr
    if e.kind == "map":
        e.rows, e.cols = shp.ny, shp.nx
        if e.x is not None:
            e.x.addr, e.x.count = shp.x_addr, shp.nx
        if e.y is not None:
            e.y.addr, e.y.count = shp.y_addr, shp.ny
    else:
        ax = curve_axis(e)
        if ax is not None:
            ax.addr, ax.count = shp.x_addr, shp.nx
        if e.rows > 1:
            e.rows, e.cols = shp.nx, 1
        else:
            e.rows, e.cols = 1, shp.nx
    if getattr(e, "source", "") == "variant" and " (variant @" in e.name:
        # a coding-variant copy is named after its value address
        # (TunerSession.variant_entities), which a resize moves
        e.name = "%s (variant @0x%X)" % (e.name.split(" (variant @")[0],
                                         shp.z_addr)


NOTE = "Resized in this project"


def _strip_note(note: str) -> str:
    parts = [p for p in (note or "").split(" | ") if not p.startswith(NOTE)]
    return " | ".join(parts)


def mark(e, stock: Tuple[int, int]) -> None:
    """Note on ``e`` (Properties, tooltip) that it holds a non-stock size."""
    base = _strip_note(e.note)
    nx, ny = dims(e)
    if (nx, ny) == tuple(stock):
        e.note = base
        return
    txt = "%s: %s (stock %s)" % (NOTE, fmt_dims(e, nx, ny),
                                 fmt_dims(e, stock[0], stock[1]))
    e.note = (base + " | " + txt) if base else txt


def is_resized(e) -> bool:
    return NOTE in (getattr(e, "note", "") or "")


def lines_up(session, e, data) -> bool:
    """Whether ``e``'s cells are where they are now in the image ``data`` as
    well -- False when the table was resized or moved between ``data`` and
    now, so cell (r, c) read from ``data`` would be another cell, an axis
    point or another table's byte. Reverting cells to an older image (Revert
    to last saved / as-read values) asks this first.

    A table this platform's codec cannot rewrite has never been resized, so
    it lines up. One it can: its record must be in ``data`` at the same place
    with the same point counts (:meth:`LayoutCodec.record`)."""
    codec = codec_for(session)
    if codec is None or data is None:
        return True
    hdr = None
    if getattr(codec, "set_start", None) is not None:
        try:
            hdr = codec.set_start(e) if set_of(session, e, codec) else None
        except Exception:
            hdr = None
        ax = curve_axis(e) if e.kind == "curve" else None
        if hdr is None and ax is not None and ax.addr == e.addr:
            hdr = e.addr - 4                # a curve showing a set's axis
    if hdr is not None:
        # a map set member: the shared axes must be where they are now, at
        # the same size, in ``data`` too (a resize rewrites them; a move
        # puts them -- and every member's cells -- somewhere else)
        try:
            now = codec.read_axes(bytes(session.binfile.data), hdr, e)
            then = codec.read_axes(bytes(data), hdr, e)
        except Exception:
            return True
        if now is None:
            return True
        return then is not None and (then.nx, then.ny, then.x_addr) == \
            (now.nx, now.ny, now.x_addr)
    try:
        now = codec.record(bytes(session.binfile.data), e)
    except Exception:
        return True
    if now is None:
        return True
    try:
        then = codec.record(bytes(data), e)
    except Exception:
        then = None
    return then is not None and (then.start, then.nx, then.ny, then.z_addr) == \
        (now.start, now.nx, now.ny, now.z_addr)


# ------------------------------------------------------------------ modifiers
def key_of(m) -> tuple:
    return (m.get("name"), m.get("group") or "", m.get("kind"))


def mods(session) -> List[dict]:
    """The layout modifiers ``session`` carries."""
    return [dict(m) for m in (getattr(session, "_layout_mods", None) or [])]


def set_mods(session, lst) -> None:
    session._layout_mods = [dict(m) for m in (lst or [])]


def merge(a, b) -> List[dict]:
    """``a`` and ``b`` as one list: one record per table, every size either
    has seen, the earliest stock size. Neither list loses anything."""
    out: Dict[tuple, dict] = {}
    for m in list(a or []) + list(b or []):
        if not isinstance(m, dict) or not m.get("name"):
            continue
        k = key_of(m)
        cur = out.get(k)
        if cur is None:
            cur = out[k] = dict(m)
            cur["sizes"] = []
            cur["reserved"] = []
        else:
            cur.setdefault("reserved", [])
            # a map set's latest pointers and members (its slots are the
            # same every time; where they lead is the bytes' business)
            for f in ("now", "when", "app", "copies", "how", "sets",
                      "members", "views"):
                if f in m:
                    cur[f] = m[f]
        for r in m.get("reserved") or []:
            if list(r) not in cur["reserved"]:
                cur["reserved"].append(list(r))
        if not cur["reserved"]:
            del cur["reserved"]
        for s in list(m.get("sizes") or []) + [m.get("now")]:
            if s and list(s) not in cur["sizes"]:
                cur["sizes"].append(list(s))
    return list(out.values())


def _record(main, plans: List[Plan], stock) -> dict:
    """The modifier describing a batch of plans for one table (its main copy
    first, then its other copies)."""
    from . import branding
    p0 = plans[0]
    rec = {"name": main.name, "group": main.group or "", "kind": main.kind,
           "mode": p0.mode, "stock": list(stock), "now": list(p0.new),
           "sizes": [list(p0.new)], "copies": len(plans) - 1,
           "when": time.strftime("%Y-%m-%d %H:%M"),
           "app": getattr(branding, "__version__", "")}
    if p0.mode == "relocate":
        # per copy, the ECU slot(s) the move re-pointed -- apply confirms by
        # reading them back, so a stock image (slots still at the old record)
        # is left alone and a relocated one follows the slot to the new record
        rec["relocate"] = [
            {"group": p.entity.group or "", "base": p.base,
             "slots": list(p.slots), "old_start": p.old_start,
             "new_start": p.new_start, "disp": p.disp,
             "new_base": p.new_start - p.disp,
             "bytes": p.reserve or codec_footprint(p)} for p in plans]
    else:
        rec["start"] = p0.start
    return rec


def _view_stock(stock, which: str) -> Tuple[int, int]:
    """The stock size of a curve showing set axis ``which``."""
    return (stock[0] if which == "x" else stock[1]), 1


def _set_record(main, plans: List[Plan], stock, prev) -> dict:
    """The modifier for a map set resize (``mode: "set"``): the members, the
    axis curves, and per dataset the pointer slots the set is reached
    through, which apply follows."""
    from . import branding
    p0 = plans[0]
    members = []
    for p in plans:
        if p.set_info is not None and members:
            break                         # the next dataset's copy begins
        if not p.view:
            members.append(p.entity.name)
    views = []
    for p in plans:
        if p.view and [p.entity.name, p.view] not in views:
            views.append([p.entity.name, p.view])
    stock_hdr = {r.get("group") or "": r.get("stock_hdr")
                 for r in (prev or {}).get("sets") or []}
    sets, reserved = [], []
    for p in plans:
        if p.set_info is None:
            continue
        r = dict(p.set_info)
        r["stock_hdr"] = stock_hdr.get(r["group"]) or r["old_hdr"]
        reserved += r.pop("reserved", [])
        sets.append(r)
    return {"name": main.name, "group": main.group or "", "kind": main.kind,
            "mode": "set", "how": p0.mode, "stock": list(stock),
            "now": list(p0.new), "sizes": [list(p0.new)],
            "copies": len(sets) - 1, "members": members, "views": views,
            "sets": sets, "reserved": reserved,
            "when": time.strftime("%Y-%m-%d %H:%M"),
            "app": getattr(branding, "__version__", "")}


def codec_footprint(p: Plan) -> int:
    """Bytes the moved record occupies at its new home (even-padded)."""
    return _even(p.shape.end - p.shape.start)


def find(session, m: dict):
    """The (main) entity a modifier is about, or None."""
    for e in getattr(session, "entities", []) or []:
        if e.name == m.get("name") and (e.group or "") == (m.get("group") or "") \
                and e.kind == m.get("kind"):
            return e
    return None


def _apply_relocate(session, m, codec, data) -> Tuple[bool, List[str]]:
    """Re-point every copy of a relocated table to wherever its ECU slot now
    says, confirming structurally: a relocated image follows the slot to the
    moved record; a stock/pre-relocate image (slot still at the old record) is
    left as the module built it. Returns (any-moved, problems)."""
    name, kind = m.get("name"), m.get("kind")
    stock = tuple(m.get("stock") or ())
    sizes = {tuple(s) for s in (m.get("sizes") or [])} | {stock}
    moved = False
    problems = []
    for r in m.get("relocate") or []:
        grp = r.get("group") or ""
        ent = [e for e in getattr(session, "entities", []) or []
               if e.name == name and e.kind == kind and (e.group or "") == grp]
        if not ent:
            problems.append("%s [%s]: not in this definition" % (name, grp))
            continue
        e = ent[0]
        slots = r.get("slots") or []
        base = int(r.get("base", 0))
        disp = int(r.get("disp", 0))
        tgt = codec.slot_target(data, int(slots[0]), base) if slots else None
        if tgt is None:
            problems.append("%s [%s]: its ECU pointer could not be read"
                            % (name, grp))
            continue
        blk = tgt
        tgt += disp                          # EDC16: block base -> record
        shp = codec.read(data, tgt, e)
        if (shp is None or (shp.nx, shp.ny) not in sizes) and disp and \
                r.get("new_base") is not None and r.get("old_start") is not None \
                and blk != int(r["new_base"]):
            # an image from before the move (the original, an older
            # version): its slot still leads to the old block, where the
            # record sits at its old place -- not at the moved block's
            # displacement (03G906021KK: 0x1D6490, not old block + 0x113C)
            shp = codec.read(data, int(r["old_start"]), e)
        if shp is None or (shp.nx, shp.ny) not in sizes:
            problems.append("%s [%s]: the record its pointer leads to is not a "
                            "size it has had" % (name, grp))
            continue
        if (shp.nx, shp.ny) != dims(e) or shp.z_addr != e.addr:
            shape_entity(e, shp)
        if (shp.nx, shp.ny) != stock and e.source not in (
                "anchored", "struct", "verified", "placed"):
            e.source = "verified"
        mark(e, stock)
        if (shp.nx, shp.ny) != stock:
            moved = True
    return moved, problems


def _apply_set(session, m, codec, data) -> Tuple[bool, List[str]]:
    """Re-shape every member of a resized map set (and its axis curves), in
    every dataset, to wherever the set's ECU pointers lead in ``data``: the
    first axis-record slot gives the axes (a size the set has had, the
    second axis's slot agreeing), each member's cell slot its cells. A stock
    or pre-resize image leads to the stock set and keeps it. Returns
    (any-not-stock, problems)."""
    kind = m.get("kind")
    stock = tuple(m.get("stock") or ())
    sizes = {tuple(s) for s in (m.get("sizes") or [])} | {stock}
    names = list(m.get("members") or [])
    moved, problems = False, []
    for r in m.get("sets") or []:
        grp = r.get("group") or ""
        here = [e for e in getattr(session, "entities", []) or []
                if (e.group or "") == grp]
        ents = {e.name: e for e in here if e.kind == kind and e.name in names}
        label = "%s [%s]" % (m.get("name"), grp)
        if not names or set(ents) != set(names):
            problems.append("%s: the set's maps are not all in this definition"
                            % label)
            continue
        base = int(r.get("base", 0))
        ax = r.get("axis_slots") or []
        cs = r.get("cell_slots") or []
        hdr = (codec.slot_target(data, int(ax[0][0]), base)
               if ax and ax[0] else None)
        e0 = ents[names[0]]
        shp = codec.read_axes(data, hdr, e0) if hdr is not None else None
        if shp is None or (shp.nx, shp.ny) not in sizes:
            problems.append("%s: the axes its pointer leads to are not a size "
                            "the set has had" % label)
            continue
        if kind == "map" and (len(ax) < 2 or not ax[1] or codec.slot_target(
                data, int(ax[1][0]), base) != shp.x_addr - 4):
            problems.append("%s: its two axis pointers do not agree" % label)
            continue
        cells = [codec.slot_target(data, int(c[0]), base) if c else None
                 for c in cs]
        if len(cells) != len(names) or None in cells:
            problems.append("%s: a member's pointer could not be read" % label)
            continue
        step = e0.bits // 8
        for nm, at in zip(names, cells):
            e = ents[nm]
            new = Shape(hdr, shp.nx, shp.ny, shp.x_addr, shp.y_addr, at,
                        at + step * shp.nx * shp.ny)
            if (shp.nx, shp.ny) != dims(e) or at != e.addr or \
                    _axes_key(e) != _axes_key_of(e, new):
                shape_entity(e, new)
            if (shp.nx, shp.ny) != stock and e.source not in (
                    "anchored", "struct", "verified", "placed"):
                e.source = "verified"
            mark(e, stock)
        for vname, which in m.get("views") or []:
            for v in here:
                if v.name == vname and v.kind == "curve":
                    vs = _view_shape(shp, which)
                    if (vs.nx, 1) != dims(v) or v.addr != vs.z_addr:
                        shape_entity(v, vs)
                    mark(v, _view_stock(stock, which))
        if (shp.nx, shp.ny) != stock or hdr != int(r.get("stock_hdr") or hdr):
            moved = True
    return moved, problems


def _axes_key_of(e, shp: Shape) -> tuple:
    if e.kind == "map":
        return (shp.x_addr, shp.y_addr)
    return (shp.x_addr,)


def follow(session, e, codec=None, stock=None) -> bool:
    """Re-shape ``e`` to whatever valid record its header holds in this
    session's image. True when it changed."""
    codec = codec or codec_for(session)
    if codec is None:
        return False
    start = codec.start_of(e)
    if start is None:
        return False
    shp = codec.read(e.binfile.data, start, e)
    if shp is None:
        return False
    changed = (shp.nx, shp.ny) != dims(e) or shp.z_addr != e.addr
    if changed:
        shape_entity(e, shp)
    if stock is not None:
        mark(e, stock)
    return changed


def _variants_of(session, names) -> List[object]:
    try:
        vs = list(session.variant_entities())
    except Exception:
        return []
    return [v for v in vs if v.name.split(" (variant @")[0] in names]


def apply(session, lst, codec: Optional[LayoutCodec] = None
          ) -> Tuple[int, List[str]]:
    """Re-apply stored modifiers after the module built ``session``'s model.

    For each, the record the resize wrote (at its recorded address) is read
    from the image: holding one of the sizes the table has had, the table is
    re-shaped to it -- also when the module placed it some other way, the
    confirmed record wins; holding the stock size, the image predates the
    resize and the table is left as the module built it. Returns
    ``(re-shaped, problems)``; the session keeps the whole list either way.
    """
    lst = merge(mods(session), lst)
    set_mods(session, lst)
    if not lst:
        return 0, []
    codec = codec or codec_for(session)
    if codec is None:
        return 0, ["the installed module cannot read resized tables"]
    data = session.binfile.data
    n, problems, with_copies = 0, [], set()
    for m in lst:
        if m.get("mode") in ("relocate", "set"):
            fn = _apply_set if m.get("mode") == "set" else _apply_relocate
            ok, probs = fn(session, m, codec, bytes(data))
            n += 1 if ok else 0
            problems += probs
            continue
        stock = tuple(m.get("stock") or ())
        sizes = {tuple(s) for s in (m.get("sizes") or [])} | {stock}
        group = [e for e in getattr(session, "entities", []) or []
                 if e.name == m.get("name") and e.kind == m.get("kind")]
        mine = [e for e in group if (e.group or "") == (m.get("group") or "")]
        if not mine:
            problems.append("%s: not in this definition" % m.get("name"))
            continue
        try:
            shp = codec.read(data, int(m["start"]), mine[0])
        except Exception:
            shp = None
        if shp is None or (shp.nx, shp.ny) not in sizes:
            problems.append("%s: no record of a size it has had at 0x%X"
                            % (m.get("name"), int(m.get("start", 0))))
            continue
        if (shp.nx, shp.ny) == stock and dims(mine[0]) == stock:
            continue                     # an image from before the resize
        for e in group:                  # the main copy, then other datasets
            if e is mine[0]:
                if (shp.nx, shp.ny) != dims(e) or shp.z_addr != e.addr:
                    shape_entity(e, shp)
                if e.source not in ("anchored", "struct", "verified", "placed"):
                    e.source = "verified"     # the record the resize wrote
                mark(e, stock)
            else:
                follow(session, e, codec, stock)
        n += 1
        if m.get("copies"):
            with_copies.add(m.get("name"))
    if with_copies:
        # coding-variant copies are derived from their main copies: derive
        # them again from the re-shaped ones, then confirm each against its
        # own record
        session._variant_entities = None
        for v in _variants_of(session, with_copies):
            m = next(x for x in lst if x.get("name") == v.name.split(" (variant @")[0])
            follow(session, v, codec, tuple(m.get("stock") or ()))
    return n, problems


def follow_headers(session, codec: Optional[LayoutCodec] = None) -> int:
    """For a session that borrowed another image's definition (a version
    saved before or after a resize, ``TunerSession.sibling``) or whose bytes
    were written back from history: re-shape every table the session's
    modifiers name to the size its record in THIS image holds. Returns how
    many changed."""
    lst = mods(session)
    if not lst:
        return 0
    codec = codec or codec_for(session)
    if codec is None:
        return 0
    n = 0
    reloc = {}                       # (name, kind) -> modifier, for relocate
    keys = {(m.get("name"), m.get("kind")) for m in lst}
    stock = {(m.get("name"), m.get("kind")): tuple(m.get("stock") or ())
             for m in lst}
    for m in lst:
        if m.get("mode") in ("relocate", "set"):
            reloc[(m.get("name"), m.get("kind"))] = m
    for m in reloc.values():
        # a moved table (or a map set) follows its ECU slot(s) wherever THIS
        # image puts it
        fn = _apply_set if m.get("mode") == "set" else _apply_relocate
        ok, _probs = fn(session, m, codec, bytes(session.binfile.data))
        n += 1 if ok else 0
    for e in getattr(session, "entities", []) or []:
        k = (e.name, e.kind)
        if k in keys and k not in reloc:
            n += follow(session, e, codec, stock[k])
    cached = getattr(session, "_variant_entities", None)
    if cached:
        for v in cached:
            k = (v.name.split(" (variant @")[0], v.kind)
            if k in keys and k not in reloc:
                n += follow(session, v, codec, stock[k])
    return n


def adopt_headers(session, keys, codec: Optional[LayoutCodec] = None
                  ) -> Tuple[int, List[str]]:
    """Tables ``keys`` (``(name, group, kind)``) of a session opened with
    another version's definition (``ovtsuite.defn.rebase``) whose headers
    differ in this image -- resized in the tune: each is re-shaped to the
    record this image holds and given a modifier, so a sibling session of
    the other version (the comparison) follows its own header back.
    Returns ``(re-shaped, problems)``."""
    if not keys:
        return 0, []
    codec = codec or codec_for(session)
    if codec is None:
        return 0, ["the installed module cannot read resized tables"]
    n, problems, recs = 0, [], []
    for name, group, kind in keys:
        e = next((x for x in getattr(session, "entities", []) or []
                  if x.name == name and x.kind == kind
                  and (x.group or "") == group), None)
        if e is None:
            continue
        stock = dims(e)
        start = codec.start_of(e)
        shp = codec.read(e.binfile.data, start, e) if start is not None else None
        if shp is None:
            problems.append("%s: no valid record at its header" % name)
            continue
        if (shp.nx, shp.ny) == stock and shp.z_addr == e.addr:
            continue
        shape_entity(e, shp)
        mark(e, stock)
        n += 1
        recs.append({"name": name, "group": group, "kind": kind,
                     "mode": "inplace", "stock": list(stock),
                     "now": [shp.nx, shp.ny], "sizes": [[shp.nx, shp.ny]],
                     "copies": 0, "start": start})
    if recs:
        set_mods(session, merge(mods(session), recs))
    return n, problems


def dumps(lst) -> bytes:
    return json.dumps({"format": FORMAT, "mods": list(lst)},
                      indent=1, sort_keys=True).encode("utf-8")


def loads(data: Optional[bytes]) -> List[dict]:
    if not data:
        return []
    try:
        d = json.loads(bytes(data).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(d, dict) or int(d.get("format", 0)) > FORMAT:
        return []
    return [m for m in d.get("mods") or [] if isinstance(m, dict)]


def apply_from_project(proj, sid, session) -> str:
    """The set's stored modifiers, applied to ``session``. A note for the
    status bar ("" when there is nothing to say)."""
    try:
        lst = proj.layout_mods(sid)
    except Exception:
        return ""
    if not lst:
        return ""
    _n, problems = apply(session, lst)
    if problems:
        return "%d resized table(s) could not be confirmed (%s)" % (
            len(problems), "; ".join(problems[:3]))
    return ""


# ------------------------------------------------------------------ write
def execute(session, plans: List[Plan], label: str = ""):
    """Write ``plans`` (one table and its copies) as ONE undo step, and
    record the modifier on the session. Returns the command."""
    from .session import Command

    class ResizeCommand(Command):
        """Bytes plus the entities' layout, undone together. The modifier
        is not undone: it lists sizes, and the bytes decide which applies."""

        def __init__(self, label, writes, entries):
            super().__init__(label, writes)
            self.entries = entries
            self.resize = True

        def apply(self, binfile):
            super().apply(binfile)
            for e, _b, a in self.entries:
                set_layout(e, a)

        def revert(self, binfile):
            super().revert(binfile)
            for e, b, _a in self.entries:
                set_layout(e, b)

        def merge(self, other):
            return False

    if not plans:
        raise ValueError("nothing to resize")
    main = plans[0].entity
    prev = next((m for m in mods(session)
                 if key_of(m) == (main.name, main.group or "", main.kind)), None)
    stock = tuple(prev["stock"]) if prev else tuple(plans[0].old)
    is_set = plans[0].set_info is not None
    writes, entries = [], []
    for p in plans:
        for a, o, nb in p.byte_writes():
            writes.append((a, 8, o, nb))
        e = p.entity
        tmp = copy.copy(e)
        _own_axes(tmp)
        shape_entity(tmp, p.shape)
        mark(tmp, _view_stock(stock, p.view) if p.view else stock)
        entries.append((e, layout_of(e), layout_of(tmp)))
    verb = "Move + resize" if plans[0].mode == "relocate" else "Resize"
    lab = label or "%s %s%s %s -> %s" % (
        verb, main.name, " and its set" if is_set else "",
        fmt_dims(main, *plans[0].old), fmt_dims(main, *plans[0].new))
    cmd = ResizeCommand(lab, writes, entries)
    n = (sum(1 for p in plans if p.set_info is not None) if is_set
         else len(plans))
    session._record(cmd, "%s (%d cop%s)" % (lab, n, "y" if n == 1 else "ies"))
    rec = (_set_record(main, plans, stock, prev) if is_set
           else _record(main, plans, stock))
    set_mods(session, merge(mods(session), [rec]))
    return cmd


__all__ = ["Shape", "LayoutCodec", "Limits", "Plan", "codec_for", "dims",
           "fmt_dims", "copies_of", "main_of", "limits", "all_limits",
           "plan_one", "relocate_one", "plan", "resample", "spread",
           "set_of", "set_main", "set_limits", "plan_set", "SetLimits",
           "execute", "apply",
           "follow", "follow_headers", "adopt_headers", "mods", "set_mods", "merge", "find",
           "dumps", "loads", "apply_from_project", "is_resized", "MAX_POINTS",
           "RELOCATE_READY", "RELOCATE_WHY"]

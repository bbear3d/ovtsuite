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
    for o in ents:
        if id(o) in skip_ids:
            continue
        s = None
        if o.kind in ("map", "curve"):
            try:
                s = codec.start_of(o)
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

    @property
    def noop(self) -> bool:
        if self.mode == "relocate":
            return False
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
    return codec.encode(data, enc_start, e, nx, ny, xs, ys, flat), nx, ny


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
    for fs, fl in codec.free_spans(session, e):
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
        else:
            for f in ("now", "when", "app", "copies"):
                if f in m:
                    cur[f] = m[f]
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
        tgt += disp                          # EDC16: block base -> record
        shp = codec.read(data, tgt, e)
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
        if m.get("mode") == "relocate":
            ok, probs = _apply_relocate(session, m, codec, bytes(data))
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
        if m.get("mode") == "relocate":
            reloc[(m.get("name"), m.get("kind"))] = m
    for m in reloc.values():
        # a moved table follows its ECU slot wherever THIS image puts it
        ok, _probs = _apply_relocate(session, m, codec, bytes(session.binfile.data))
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
    writes, entries = [], []
    for p in plans:
        for a, o, nb in p.byte_writes():
            writes.append((a, 8, o, nb))
        e = p.entity
        tmp = copy.copy(e)
        _own_axes(tmp)
        shape_entity(tmp, p.shape)
        mark(tmp, stock)
        entries.append((e, layout_of(e), layout_of(tmp)))
    verb = "Move + resize" if plans[0].mode == "relocate" else "Resize"
    lab = label or "%s %s %s -> %s" % (
        verb, main.name, fmt_dims(main, *plans[0].old),
        fmt_dims(main, *plans[0].new))
    cmd = ResizeCommand(lab, writes, entries)
    session._record(cmd, "%s (%d cop%s)" % (lab, len(plans),
                                            "y" if len(plans) == 1 else "ies"))
    set_mods(session, merge(mods(session), [_record(main, plans, stock)]))
    return cmd


__all__ = ["Shape", "LayoutCodec", "Limits", "Plan", "codec_for", "dims",
           "fmt_dims", "copies_of", "main_of", "limits", "all_limits",
           "plan_one", "relocate_one", "plan", "resample", "spread",
           "execute", "apply",
           "follow", "follow_headers", "mods", "set_mods", "merge", "find",
           "dumps", "loads", "apply_from_project", "is_resized", "MAX_POINTS",
           "RELOCATE_READY", "RELOCATE_WHY"]

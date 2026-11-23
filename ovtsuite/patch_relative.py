# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Relative patch entries: a change, not a set of values.

An ordinary :class:`~ovtsuite.patch.PatchEntry` carries the values a table
should end up with and writes every cell. That is right for one software
version, but wrong for a family of them whose stock tables differ. Timing is
the usual case: "+1 degree at full load" means the same on every file, while
the absolute values a patch would carry do not.

A relative entry (``PatchEntry.mode == "relative"``) carries a list of
operations in ``PatchEntry.ops``. Each one works on the target's OWN current
values, picks its cells by axis VALUE (rpm, mg, hPa -- never by index, so a
target with other breakpoints is still addressed correctly), and only the
cells whose value actually changes are written. Each op is a dict:

``op``
    ``"add"`` (value + v), ``"scale"`` (value * v), ``"floor"`` (at least
    v), ``"cap"`` (at most v), ``"set"`` (exactly v), ``"add_grid"`` (add a
    captured grid of differences) or ``"scale_grid"`` (multiply by a
    captured grid of ratios) -- see :func:`delta_entry`.
``value``
    the number ``v``; or ``curve``: ``[[axis value, v], ...]`` interpolated
    along ``along`` (``"x"``, the default, or ``"y"``) and held flat beyond
    its ends, so "floor 330 Nm from 1000 rpm, 370 from 2000" is one op.
``x`` / ``y``
    optional ``[lo, hi]`` (inclusive, axis units; ``null`` for open) picking
    the cells. An axis left out selects all of it. A constant has no axes:
    its one cell is always selected.

Ops run in order, each on the result of the one before. ``variants`` (on by
default) applies the entry to every coding-variant copy of the named table
too -- on a file where the timing maps exist twice, both get the change --
and a target missing the copies a source had still gets the base table.
``only_coding`` narrows that to the copies a kind of coding selects: with
``"automatic (TC)"`` a driver-wish cap reaches the copy an automatic reads
and leaves the manual and DSG copies alone. A table every coding shares has
no coding of its own, so such a filter skips it.

``add_grid`` holds ``x_axis``/``y_axis``/``grid``: the differences between
two binaries, cell by cell. On a target with the same breakpoints it adds
exactly those numbers; elsewhere each target cell takes the difference at
its own breakpoints (bilinear, held flat outside the grid). Cells whose
difference is zero are left alone, so a target's own values there survive.
"""

from typing import List, Optional, Tuple

OPS = ("add", "scale", "floor", "cap", "set", "add_grid", "scale_grid")
GRID_OPS = ("add_grid", "scale_grid")


class OpError(ValueError):
    """An op the engine cannot carry out (unknown name, missing value)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _interp(xs, ys, x):
    if not xs:
        raise OpError("empty curve")
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            span = xs[i] - xs[i - 1]
            t = (x - xs[i - 1]) / span if span else 0.0
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _bracket(axis, v):
    """(i0, i1, t) locating ``v`` on ``axis`` (held flat outside)."""
    if len(axis) == 1 or v <= axis[0]:
        return 0, 0, 0.0
    if v >= axis[-1]:
        n = len(axis) - 1
        return n, n, 0.0
    for i in range(1, len(axis)):
        if v <= axis[i]:
            span = axis[i] - axis[i - 1]
            return i - 1, i, ((v - axis[i - 1]) / span if span else 0.0)
    n = len(axis) - 1
    return n, n, 0.0


def _grid_at(op, x, y):
    gx = op.get("x_axis") or [0.0]
    gy = op.get("y_axis") or [0.0]
    g = op["grid"]
    # an exact breakpoint match takes the captured number as it is
    try:
        if x in gx and y in gy:
            return g[gy.index(y)][gx.index(x)]
    except (IndexError, ValueError):
        pass
    c0, c1, tx = _bracket(gx, x)
    r0, r1, ty = _bracket(gy, y)
    a = g[r0][c0] + tx * (g[r0][c1] - g[r0][c0])
    b = g[r1][c0] + tx * (g[r1][c1] - g[r1][c0])
    return a + ty * (b - a)


def _in(rng, v) -> bool:
    if not rng:
        return True
    lo, hi = rng[0], rng[1]
    return (lo is None or v >= lo - 1e-9) and (hi is None or v <= hi + 1e-9)


def _operand(op, x, y) -> float:
    curve = op.get("curve")
    if curve:
        along = op.get("along", "x")
        pts = sorted((float(a), float(b)) for a, b in curve)
        return _interp([p[0] for p in pts], [p[1] for p in pts],
                       x if along == "x" else y)
    if op.get("value") is None:
        raise OpError("op %r has neither value nor curve" % op.get("op"))
    return float(op["value"])


def _one(op, old, x, y) -> float:
    kind = op.get("op")
    if kind == "add_grid":
        return old + _grid_at(op, x, y)
    if kind == "scale_grid":
        # a cell the source held at zero has no ratio; its change rides in
        # the op's "add" grid instead (see delta_entry)
        new = old * _grid_at(op, x, y)
        if op.get("add"):
            new += _grid_at(dict(op, grid=op["add"]), x, y)
        return new
    v = _operand(op, x, y)
    if kind == "add":
        return old + v
    if kind == "scale":
        return old * v
    if kind == "floor":
        return max(old, v)
    if kind == "cap":
        return min(old, v)
    if kind == "set":
        return v
    raise OpError("unknown op %r (expected one of %s)" % (kind, ", ".join(OPS)))


def _axis_vals(e, which) -> List[float]:
    try:
        vals = e.x_values() if which == "x" else e.y_values()
    except Exception:                                    # noqa: BLE001
        vals = []
    return list(vals or [])


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------

def cells_for(entity, ops) -> List[Tuple[int, int, float]]:
    """The ``(row, col, new value)`` writes ``ops`` make on ``entity`` -- only
    cells whose value changes. Raises :class:`OpError` for a malformed op."""
    xs = _axis_vals(entity, "x")
    ys = _axis_vals(entity, "y")
    vals = entity.values()
    out = []
    for r in range(entity.rows):
        y = ys[r] if r < len(ys) else 0.0
        for c in range(entity.cols):
            x = xs[c] if c < len(xs) else 0.0
            old = vals[r][c]
            new = old
            for op in ops:
                if entity.kind != "constant" and not (
                        _in(op.get("x"), x) and _in(op.get("y"), y)):
                    continue
                new = _one(op, new, x, y)
            if abs(new - old) > 1e-9:
                out.append((r, c, new))
    return out


def targets_for(session, entry, find_entity, variants_of) -> list:
    """The tables a relative ``entry`` applies to on ``session``: the one it
    names, plus every coding-variant copy of it when ``entry.variants``."""
    base = entry.name.split(" (variant @", 1)[0]
    found = []
    te = find_entity(session, entry.name, entry.coding)
    if te is not None:
        found.append(te)
    if getattr(entry, "variants", True):
        for v in variants_of(session):
            if (v.name or "").split(" (variant @", 1)[0] == base \
                    and all(v is not f for f in found):
                found.append(v)
    want = (getattr(entry, "only_coding", "") or "").lower()
    if want:
        found = [t for t in found
                 if want in (getattr(t, "coding", "") or "").lower()]
    return found


def describe(ops) -> str:
    """A one-line summary of ``ops`` for lists and ``patch-inspect``."""
    parts = []
    for op in ops or ():
        kind = op.get("op", "?")
        if kind == "add_grid":
            g = op.get("grid") or []
            nz = [v for row in g for v in row if v]
            parts.append("add grid (%d cell(s)%s)" % (
                len(nz), ", %+.3g..%+.3g" % (min(nz), max(nz)) if nz else ""))
            continue
        if kind == "scale_grid":
            g = op.get("grid") or []
            nz = [v for row in g for v in row if abs(v - 1) > 1e-12]
            parts.append("scale grid (%d cell(s)%s)" % (
                len(nz), ", %+.3g%%..%+.3g%%" % ((min(nz) - 1) * 100,
                                                 (max(nz) - 1) * 100)
                if nz else ""))
            continue
        if op.get("curve"):
            v = "curve along %s" % op.get("along", "x")
        else:
            v = "%g" % op.get("value", 0)
            if kind == "add" and not v.startswith("-"):
                v = "+" + v
            if kind == "scale":
                v = "x" + v
        where = []
        for ax in ("x", "y"):
            rng = op.get(ax)
            if rng:
                lo, hi = rng[0], rng[1]
                where.append("%s %s..%s" % (ax, "" if lo is None else "%g" % lo,
                                            "" if hi is None else "%g" % hi))
        parts.append("%s %s%s" % (kind, v, " @ " + ", ".join(where) if where else ""))
    return "; ".join(parts)


def relative_entry(name: str, ops, *, kind: str = "map", label: str = "",
                   description: str = "", units: str = "",
                   variants: bool = True, coding: str = "",
                   only_coding: str = ""):
    """A relative :class:`~ovtsuite.patch.PatchEntry` named ``name`` doing
    ``ops`` (validated here, so a bad op fails at build time, not apply)."""
    from .patch import PatchEntry
    for op in ops:
        if op.get("op") not in OPS:
            raise OpError("unknown op %r (expected one of %s)"
                          % (op.get("op"), ", ".join(OPS)))
        if op["op"] in GRID_OPS:
            if not op.get("grid"):
                raise OpError("%s needs a grid" % op["op"])
        elif op.get("value") is None and not op.get("curve"):
            raise OpError("op %r needs a value or a curve" % op["op"])
    first = "relative: " + describe(ops)
    if only_coding:
        first += " [only %s]" % only_coding
    return PatchEntry(name=name, kind=kind, rows=0, cols=0, values=[],
                      units=units, label=label, description=description,
                      changed_cells=0, total_cells=0, first_change=first,
                      mode="relative", ops=[dict(o) for o in ops],
                      variants=variants, coding=coding,
                      only_coding=only_coding)


def delta_entry(primary_entity, comparison_entity, *, x=None, y=None,
                variants: bool = True, percent: bool = False):
    """A relative entry taking a table from primary to comparison (both the
    same shape) as a CHANGE, or ``None`` when nothing differs. ``x`` / ``y``
    limit it to a region, as an op does.

    By default the change is the difference (``add_grid``): the same number
    of degrees, Nm or hPa added to whatever the target holds. ``percent``
    carries it as a ratio instead (``scale_grid``): the same percentage, so
    a target with larger stock values gets a proportionally larger change.
    A cell the primary holds at zero has no ratio; its difference is carried
    as an addition alongside."""
    pe, ce = primary_entity, comparison_entity
    if (pe.rows, pe.cols) != (ce.rows, ce.cols):
        return None
    xs = _axis_vals(pe, "x")
    ys = _axis_vals(pe, "y")
    a, b = pe.values(), ce.values()
    grid = []
    for r in range(pe.rows):
        yv = ys[r] if r < len(ys) else 0.0
        row = []
        for c in range(pe.cols):
            xv = xs[c] if c < len(xs) else 0.0
            d = b[r][c] - a[r][c]
            if not (_in(x, xv) and _in(y, yv)) or abs(d) < 1e-9:
                d = 0.0
            row.append(d)
        grid.append(row)
    if not any(v for row in grid for v in row):
        return None
    if percent:
        ratio, extra = [], []
        for r in range(pe.rows):
            rr, er = [], []
            for c in range(pe.cols):
                d = grid[r][c]
                if d and abs(a[r][c]) > 1e-12:
                    rr.append((a[r][c] + d) / a[r][c])
                    er.append(0.0)
                else:
                    rr.append(1.0)
                    er.append(d)
            ratio.append(rr)
            extra.append(er)
        op = {"op": "scale_grid", "x_axis": xs or [0.0],
              "y_axis": ys or [0.0], "grid": ratio}
        if any(v for row in extra for v in row):
            op["add"] = extra
    else:
        op = {"op": "add_grid", "x_axis": xs or [0.0], "y_axis": ys or [0.0],
              "grid": grid}
    name = ce.name.split(" (variant @", 1)[0] if variants else ce.name
    return relative_entry(name, [op], kind=ce.kind,
                          label=getattr(ce, "label", "") or "",
                          description=getattr(ce, "description", "") or "",
                          units=getattr(ce, "units", "") or "",
                          variants=variants)


def apply_entry(target, pe, res, *, find_entity, variants_of, trusted,
                disposition: str, apply_word: str) -> None:
    """Apply one relative entry to ``target`` inside the caller's
    transaction, recording the outcome on ``res`` (an ApplyResult)."""
    tables = targets_for(target, pe, find_entity, variants_of)
    if not tables:
        only = getattr(pe, "only_coding", "")
        if only and find_entity(target, pe.name, pe.coding) is not None:
            # the table is there; no copy of it is read by that kind of
            # coding, so this entry does not concern this file
            res.warnings.append("%s: no copy read by a %r coding in this "
                                "file -- left as it is" % (pe.name, only))
            return
        res.skipped.append((pe.name, "not identified in this target"))
        return
    wrote = 0
    for te in tables:
        if not trusted(te) and disposition != apply_word:
            res.skipped.append(
                (te.name, "target address is %r (not established) and was "
                          "not confirmed" % te.source))
            continue
        try:
            cells = cells_for(te, pe.ops or [])
        except OpError as exc:
            res.skipped.append((pe.name, str(exc)))
            return
        if not cells:
            continue
        touched = getattr(res, "touched", set())
        if te.addr not in touched and \
                bytes(target.binfile.data[te.addr:te.end]) != \
                bytes(target.original[te.addr:te.end]):
            res.conflicts += 1
        target.edit_cells(te, cells)
        touched.add(te.addr)
        wrote += 1
        if not trusted(te):
            res.warnings.append(
                "%s applied at a %r (unestablished) target address, "
                "confirmed by the caller" % (te.name, te.source))
    if wrote:
        res.applied.append(pe.name)
    elif not any(s[0] in (pe.name,) + tuple(t.name for t in tables)
                 for s in res.skipped):
        res.skipped.append((pe.name, "already as the patch asks -- nothing "
                                     "to change"))

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Where a table's cells are stored: the address the status bar shows for
the selected cells (:mod:`ovtsuite.gui_address`).

No Qt here. A cell is named as ``TableModel._kind`` names it --
``("z", r, c)``, ``("x", i, 0)``, ``("cz", r, c)`` ... -- and its address
is the first byte of its value in the binary, or None where nothing is
stored (a corner, the comparison banner, an axis a definition gives as
numbers, a comparison value resampled onto the primary's axes).
"""

from __future__ import annotations

from typing import Iterable, Optional


def axis_addr(ax, i: int) -> Optional[int]:
    """Point ``i`` of an axis, or None (no axis, or one not in the binary)."""
    if ax is None or getattr(ax, "addr", None) is None:
        return None
    if not 0 <= i < max(1, int(getattr(ax, "count", 1) or 1)):
        return None
    step = 4 if getattr(ax, "is_float", False) else max(1, ax.bits // 8)
    return ax.addr + i * step


def z_addr(e, r: int, c: int) -> Optional[int]:
    """Z cell (r, c) of an entity (its storage order taken care of)."""
    if e is None or getattr(e, "addr", None) is None:
        return None
    if not (0 <= r < e.rows and 0 <= c < e.cols):
        return None
    return e._cell_addr(r, c)


def _cmp_turned(e, cp) -> bool:
    """The comparison is a 1-D table stored the other way round (Nx1 laid
    over 1xN): the grid shows it turned (transfer.build_overlay)."""
    return (1 in (e.rows, e.cols) and 1 in (cp.rows, cp.cols)
            and (e.rows, e.cols) != (1, 1) and (cp.rows, cp.cols) != (1, 1)
            and (e.rows == 1) != (cp.rows == 1))


def cell_addr(entity, kind: str, a: int, b: int, compare=None,
              overlay_mode: Optional[str] = None) -> Optional[int]:
    """The address of one grid cell. ``compare`` and ``overlay_mode`` (the
    TableModel's ``compare`` and ``overlay.mode``) are for the comparison
    block's cells: a value resampled onto the primary's axes is stored
    nowhere, so it has no address."""
    if entity is None:
        return None
    if kind == "z":
        return z_addr(entity, a, b)
    if kind == "x":
        return axis_addr(entity.x, a)
    if kind == "y":
        return axis_addr(entity.y, a)
    if kind not in ("cz", "cx", "cy") or compare is None \
            or overlay_mode not in ("direct", "axis-mismatch"):
        return None
    turned = _cmp_turned(entity, compare)
    if kind == "cz":
        return z_addr(compare, b, a) if turned else z_addr(compare, a, b)
    ax = compare.x if kind == "cx" else compare.y
    if turned:
        ax = compare.y if kind == "cx" else compare.x
    return axis_addr(ax, a)


def describe(addrs: Iterable[Optional[int]]) -> str:
    """The status bar's text: ``0x1A2B3C`` for one cell, ``0x1A2B3C–0x1A2B5A
    (16 cells)`` for several (the lowest and highest address); "" when none
    of them is stored anywhere."""
    got = sorted({a for a in addrs if a is not None})
    if not got:
        return ""
    if len(got) == 1:
        return "0x%06X" % got[0]
    return "0x%06X–0x%06X (%d cells)" % (got[0], got[-1], len(got))

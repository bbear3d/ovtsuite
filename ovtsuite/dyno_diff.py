# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What changed between two dyno runs, and why.

The engine-speed range is cut where the governing limit of either run
changes. For each band: which limit governs in each, how far power, torque
and quantity moved, and which of the maps behind those limits differ
between the two files (cells changed, by how much) -- the map that moved
the curve, to open from the window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import dyno as _dy


@dataclass
class MapChange:
    """One map behind a limit, as it differs between the two files."""
    entity: object                  # in run A's file (to open)
    label: str
    cells: int                      # cells that differ
    total: int
    largest: float                  # the largest difference (A - B), its units
    units: str = ""
    axes: bool = False              # the breakpoints differ too

    def text(self) -> str:
        if self.cells == 0 and self.axes:
            return "%s: its breakpoints moved" % self.label
        bits = "%s: %d of %d cells, up to %+g%s" % (
            self.label, self.cells, self.total, round(self.largest, 2),
            (" " + self.units) if self.units else "")
        return bits + (", breakpoints moved" if self.axes else "")


@dataclass
class Band:
    """A stretch of engine speed with the same governing limits."""
    rpm_from: float
    rpm_to: float
    limit_a: str                    # limiter key in run A
    limit_b: str                    # in run B ("" where B has no point)
    d_power: float                  # kW, A - B, where they differ most
    d_torque: float                 # Nm
    d_q: float                      # mg/stroke
    at: float                       # the engine speed of those differences
    maps: List[MapChange] = field(default_factory=list)


def _values(e) -> Optional[List[List[float]]]:
    try:
        v = e.values()
    except Exception:
        return None
    if isinstance(v, (int, float)):
        return [[float(v)]]
    return v


def _axes(e) -> Tuple[tuple, tuple]:
    out = []
    for f in ("x_values", "y_values"):
        try:
            out.append(tuple(getattr(e, f)()))
        except Exception:
            out.append(())
    return out[0], out[1]


def _counterpart(e, session):
    """The same table in ``session``: by name and group, else by name."""
    if session is None:
        return None
    same = [x for x in getattr(session, "entities", []) or [] if x.name == e.name]
    for x in same:
        if (x.group or "") == (e.group or ""):
            return x
    return same[0] if same else None


def map_change(e, other) -> Optional[MapChange]:
    """How table ``e`` differs from ``other`` (None when they are the same,
    or can't be compared cell for cell)."""
    a, b = _values(e), _values(other) if other is not None else None
    if a is None or b is None:
        return None
    label = getattr(e, "title", "") or e.name
    if len(a) != len(b) or any(len(r) != len(s) for r, s in zip(a, b)):
        return MapChange(e, label + " (resized)", 0, sum(len(r) for r in a), 0.0,
                         getattr(e, "units", "") or "", True)
    diffs = [x - y for r, s in zip(a, b) for x, y in zip(r, s) if abs(x - y) > 1e-9]
    axes = _axes(e) != _axes(other)
    if not diffs and not axes:
        return None
    big = max(diffs, key=abs) if diffs else 0.0
    return MapChange(e, label, len(diffs), sum(len(r) for r in a), big,
                     getattr(e, "units", "") or "", axes)


def bands(a: _dy.Curve, b: _dy.Curve, session_a=None, session_b=None) -> List[Band]:
    """The bands of ``a`` against ``b`` (each an engine-speed stretch with
    the same pair of governing limits), with the maps behind those limits
    that differ between the two sessions."""
    if a is None or not a.points:
        return []
    at_b = {round(p.rpm): p for p in (b.points if b is not None else [])}
    out: List[Band] = []
    cache: Dict[str, List[MapChange]] = {}

    def maps_for(key: str) -> List[MapChange]:
        if key not in cache:
            lim = a.limiters.get(key)
            got = []
            for e in (lim.entities if lim is not None else ()):
                ch = map_change(e, _counterpart(e, session_b))
                if ch is not None:
                    got.append(ch)
            cache[key] = got
        return cache[key]

    for p in a.points:
        q = at_b.get(round(p.rpm))
        lb = q.limiter if q is not None else ""
        dp = p.power - q.power if q is not None else 0.0
        dt = p.torque - q.torque if q is not None else 0.0
        dq = p.q - q.q if q is not None else 0.0
        if out and out[-1].limit_a == p.limiter and out[-1].limit_b == lb:
            bd = out[-1]
            bd.rpm_to = p.rpm
            if abs(dt) > abs(bd.d_torque):
                bd.d_power, bd.d_torque, bd.d_q, bd.at = dp, dt, dq, p.rpm
        else:
            out.append(Band(p.rpm, p.rpm, p.limiter, lb, dp, dt, dq, p.rpm))
    for bd in out:
        seen = set()
        for key in (bd.limit_a, bd.limit_b):
            for ch in maps_for(key) if key else []:
                if id(ch.entity) not in seen:
                    seen.add(id(ch.entity))
                    bd.maps.append(ch)
    return out


def summary(bands_: Sequence[Band], a: _dy.Curve, b: _dy.Curve) -> str:
    """One line: where the two differ most, and the map behind it."""
    if not bands_:
        return ""
    bd = max(bands_, key=lambda x: abs(x.d_torque))
    if abs(bd.d_torque) < 0.5:
        return "The two runs are the same at full load."
    lim = a.limiters.get(bd.limit_a)
    why = bd.maps[0].label if bd.maps else (lim.label if lim else bd.limit_a)
    return "Most difference at %d rpm: %+.0f Nm (%+.1f mg), %s." % (
        bd.at, bd.d_torque, bd.d_q, why)


__all__ = ["Band", "MapChange", "bands", "map_change", "summary"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Turn data logs into aligned per-role sample arrays.

Every row of a log is one poll cycle of the logger (all selected measuring
blocks, read back to back), so the cells of a row are treated as simultaneous.
Several logs are kept as separate *runs*: derivatives and time windows never
span two files.

Units are normalised to the calibration's own: pressure in hPa (mbar), speed
in rpm, quantity in mg/stroke, temperatures in deg C. The only conversion that
happens in practice is a pressure logged in kPa or bar by a foreign tool; it is
decided from the unit string the log carries and, when that is empty, from the
magnitude of the column (an absolute boost pressure cannot be 2.1 hPa).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .roles import ROLES, Resolution, resolve_log

_PRESSURE_ROLES = ("boost", "boost_spec", "atm")


def _pressure_factor(unit: str, values: Sequence[Optional[float]]) -> float:
    u = (unit or "").strip().lower()
    if "kpa" in u:
        return 10.0
    if u in ("bar", "bar abs", "bara") or (u.startswith("bar") and "mbar" not in u):
        return 1000.0
    if "psi" in u:
        return 68.9476
    if u in ("mbar", "hpa") or "mbar" in u or "hpa" in u:
        return 1.0
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 1.0
    med = vals[len(vals) // 2]
    if med < 10:
        return 1000.0
    if med < 400:
        return 10.0
    return 1.0


@dataclass
class Run:
    """One log file's worth of samples."""

    name: str
    t: List[float] = field(default_factory=list)                      # seconds
    data: Dict[str, List[Optional[float]]] = field(default_factory=dict)
    resolution: Optional[Resolution] = None

    def __len__(self):
        return len(self.t)

    def has(self, role: str) -> bool:
        col = self.data.get(role)
        return bool(col) and any(v is not None for v in col)

    def get(self, role: str, i: int) -> Optional[float]:
        col = self.data.get(role)
        return col[i] if col is not None else None

    def rate(self) -> float:
        """Median sample rate in Hz (0 when unknown)."""
        if len(self.t) < 3:
            return 0.0
        d = sorted(b - a for a, b in zip(self.t, self.t[1:]) if b > a)
        if not d:
            return 0.0
        mid = d[len(d) // 2]
        return 1.0 / mid if mid > 0 else 0.0

    def duration(self) -> float:
        return (self.t[-1] - self.t[0]) if len(self.t) > 1 else 0.0


def run_from_log(log, namer=None, name: str = "") -> Run:
    res = resolve_log(log, namer)
    run = Run(name or getattr(log, "path", "") or "log", resolution=res)
    run.t = log.seconds()
    n = len(run.t)
    for role, cols in res.columns.items():
        # Most specific signal first (ROLES order), then column order.
        prio = {sig: k for k, sig in enumerate(ROLES.get(role, ()))}
        cols = sorted(cols, key=lambda c: (prio.get(res.signals[c], 99), c))
        merged: List[Optional[float]] = [None] * n
        for c in cols:                      # first column wins, the rest fill gaps
            col = log.column(c)
            if role in _PRESSURE_ROLES:     # each column in its own unit
                k = _pressure_factor(log.channels[c].unit, col)
                if k != 1.0:
                    col = [None if v is None else v * k for v in col]
            for i in range(n):
                if merged[i] is None and i < len(col):
                    merged[i] = col[i]
        run.data[role] = merged
    return run


def runs_from_paths(paths: Sequence[str], namer=None) -> List[Run]:
    from ..datalog import logfile as _lf
    import os
    out = []
    for p in paths:
        log = _lf.load(p)
        out.append(run_from_log(log, namer, os.path.basename(p)))
    return out

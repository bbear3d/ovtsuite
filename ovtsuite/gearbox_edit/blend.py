# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The effective (blended) line a mode drives on.

The TCU interpolates up to four schedules, each read at the current pedal
(8K0927158J 0xBB935C; the 6HP19's D measured as 0.58 x SKL 11 + 0.43 x SKL 16).
The weights are an ASSUMPTION the user states or a log fit supplies -- no edit
depends on them; they only draw the line the driver feels.

Useful property: an offset, a scale or a pedal slide applied identically to
every corner moves this line by exactly that amount, whatever the weights.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import ScheduleSet, interp


def effective_line(sset: ScheduleSet, grid: Dict[int, List[float]],
                   corners: Sequence[Tuple[int, float]], line: int,
                   pedal: Sequence[float]) -> List[float]:
    """Blend of ``corners`` ``[(schedule index, weight), ...]`` for ``line``
    at each ``pedal`` %. Weights are normalised to sum to 1."""
    total = sum(w for _, w in corners) or 1.0
    out = []
    for p in pedal:
        acc = 0.0
        for i, w in corners:
            s = sset.schedules[i]
            col = [grid[i][r * s.cols + line] for r in range(s.rows)]
            acc += (w / total) * interp(s.pedal, col, p)
        out.append(acc)
    return out


@dataclass
class Blend:
    """A driving mode's effective line: weighted corner schedules.

    ``corners`` are ``(schedule key, weight)``, e.g. D on the 6HP19 that was
    measured: ``[("SKL 11.0", 0.58), ("SKL 16.0", 0.43)]``. ``programs`` and
    ``dsp`` say which logged program values it models, so a log overlay or a
    fit uses the right events. Kept in the project (binary set
    ``extra["blends"]``) as plain dicts.
    """
    name: str
    corners: List[Tuple[str, float]] = field(default_factory=list)
    programs: Tuple[int, ...] = ()
    dsp: Optional[int] = None
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "corners": [[k, w] for k, w in self.corners],
                "programs": list(self.programs), "dsp": self.dsp,
                "source": self.source, "note": self.note}

    @classmethod
    def from_dict(cls, d: dict) -> "Blend":
        return cls(str(d.get("name") or "Blend"),
                   [(str(k), float(w)) for k, w in d.get("corners") or []],
                   tuple(int(p) for p in d.get("programs") or ()),
                   None if d.get("dsp") in (None, "") else int(d["dsp"]),
                   str(d.get("source") or ""), str(d.get("note") or ""))

    def indices(self, sset: ScheduleSet) -> List[Tuple[int, float]]:
        """``(schedule index, weight)`` for the corners this set has."""
        keys = {s.key: i for i, s in enumerate(sset.schedules)}
        return [(keys[k], w) for k, w in self.corners if k in keys and w > 0]

    def total(self) -> float:
        return sum(w for _, w in self.corners)

    def describe(self) -> str:
        return " + ".join("%.2f·%s" % (w, k) for k, w in self.corners if w > 0)


def blend_pedal(sset: ScheduleSet, corners: Sequence[Tuple[int, float]]) -> List[float]:
    """The pedal points to draw a blend at: every corner's own breakpoints."""
    pts = sorted({round(p, 3) for i, _ in corners for p in sset.schedules[i].pedal})
    return pts

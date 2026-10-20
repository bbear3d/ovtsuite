# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""An edit: the schedules as they are, a list of operations, a list of
checks -> what would change, what the checks say, and one undoable plan.

Nothing touches the image until :meth:`Edit.apply`, which writes every changed
cell through one ``autotune.Plan`` -- one transaction, one undo step, however
many schedules it spans. Check findings travel with the plan as warnings; none
of them blocks it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .checks import Check, Finding
from .model import ScheduleSet

Key = Tuple[int, int, int]


@dataclass
class Change:
    key: Key
    old: float
    new: float
    why: List[str]


@dataclass
class Result:
    grid: Dict[int, List[float]]
    changes: List[Change]
    findings: List[Finding]

    @property
    def cells(self) -> int:
        return len(self.changes)

    @property
    def schedules(self) -> int:
        return len({c.key[0] for c in self.changes})

    def summary(self, sset: ScheduleSet) -> str:
        if not self.changes:
            return "nothing would change"
        up = sum(1 for c in self.changes if c.new > c.old)
        lines = sorted({sset.line(c.key[2]).label for c in self.changes})
        return "%d cells in %d schedules (%d raised, %d lowered) on %s" % (
            self.cells, self.schedules, up, self.cells - up, ", ".join(lines))


class Edit:
    """Operations and checks over one :class:`ScheduleSet`.

    ``origin`` is read from the entities when the edit is made, so a preview
    always starts from what the image holds now.
    """

    def __init__(self, sset: ScheduleSet, ops: Sequence = (),
                 checks: Sequence[Check] = ()):
        self.sset = sset
        self.ops = list(ops)
        self.checks = list(checks)
        self.origin = {i: s.values() for i, s in enumerate(sset.schedules)}

    def compute(self) -> Result:
        grid = {i: list(v) for i, v in self.origin.items()}
        why: Dict[Key, List[str]] = {}
        for op in self.ops:
            for k, reason in op.apply(grid, self.sset).items():
                why.setdefault(k, []).append(reason)
        changed = {k: "; ".join(v) for k, v in why.items()}
        findings: List[Finding] = []
        for chk in self.checks:
            findings.extend(chk.run(self.sset, self.origin, grid, changed))
        changes = []
        for i, vals in grid.items():
            s = self.sset.schedules[i]
            for k, (a, b) in enumerate(zip(self.origin[i], vals)):
                if a != b:
                    key = (i, k // s.cols, k % s.cols)
                    changes.append(Change(key, a, b, why.get(key, ["auto-fix"])))
        return Result(grid, changes, findings)

    def to_plan(self, session, label: str, result: Optional[Result] = None):
        from ..autotune.plan import Plan, WARNING, NOTE
        res = result or self.compute()
        plan = Plan(session, label)
        for ch in res.changes:
            i, r, c = ch.key
            s = self.sset.schedules[i]
            plan.set(s.entity, r, s.col_offset + c, ch.new, "; ".join(ch.why))
        for f in res.findings:
            plan.add_problem(WARNING if f.severity == "warning" else NOTE,
                             f.code, f.message + (" (auto-fixed)" if f.fixed else ""))
        plan._validated = True          # advisory only: nothing here blocks
        return plan

    def apply(self, session, label: str, result: Optional[Result] = None) -> int:
        """Write the edit as one undoable step. Returns cells written."""
        plan = self.to_plan(session, label, result)
        return plan.apply(force=True) or 0

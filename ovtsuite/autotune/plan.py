# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A plan: what an auto-tune would write, before it writes anything.

The order is always **resolve -> plan -> validate -> apply**, and nothing
touches the image until the last step. That separation is the whole safety
argument: every check in `validate` runs against the values the plan *would*
produce, so a profile that would put an upshift point below its own downshift
point is caught while it is still a list of tuples.

Applying is one transaction. A plan that touches nine maps undoes in one
keystroke, and a failure part-way through rolls back rather than leaving a
half-tuned gearbox calibration on disk.

Ported from hp6tuner unchanged in shape. What differs is only what a target
is: an :class:`~ovtsuite.model.Entity` rather than hp6tuner's ``Map``, so the
plan reads and clamps in display units through the entity's own equation
instead of raw counts. That makes a clamp mean what it says -- ``KF_PKAB12``
holds +-32.767 bar, not +-32767 -- and it is why ``Entity.clamp`` had to
exist before this module could.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..model import values_equal

ERROR = "error"
WARNING = "warning"
NOTE = "note"

_RANK = {ERROR: 0, WARNING: 1, NOTE: 2}


@dataclass(frozen=True)
class Problem:
    """Something the validator found. Only ``error`` blocks an apply."""

    severity: str
    code: str
    message: str
    where: str = ""

    def __str__(self):
        where = f" [{self.where}]" if self.where else ""
        return f"{self.severity.upper()}: {self.message}{where}"


@dataclass(frozen=True)
class PlannedWrite:
    """One cell, and why."""

    target: object          # Entity
    row: int
    col: int
    old: float
    new: float
    reason: str = ""
    requested: Optional[float] = None
    """What the recipe asked for, before the map's own range and element width
    were applied. ``None`` when the plan set the value directly."""

    @property
    def was_clamped(self) -> bool:
        """True only when the value hit the map's *range*, not its precision.

        Rounding 1649.55 to 1650 in a 16-bit integer map is quantisation and
        happens on nearly every scale operation; being told about it 4,740
        times is noise that hides the one write that really did run off the
        end of the range.
        """
        if self.requested is None:
            return False
        low, high = self.target.value_range
        return self.requested < low or self.requested > high

    @property
    def key(self) -> Tuple[int, int, int]:
        return (id(self.target), self.row, self.col)

    @property
    def changes(self) -> bool:
        return not values_equal(self.old, self.new)

    def __str__(self):
        # A map found structurally has no symbol; its label says where it
        # is, which is the only handle anyone has on it.
        name = (getattr(self.target, "name", "")
                or getattr(self.target, "label", "") or "?")
        why = f"  ({self.reason})" if self.reason else ""
        return f"{name}[{self.row},{self.col}] {self.old} -> {self.new}{why}"


class Plan:
    """An ordered set of intended cell writes, not yet applied.

    Writing the same cell twice replaces the earlier intent rather than
    stacking, so a profile that sets a value and then clamps it reads as one
    write with the final value -- which is also what the validator needs to
    see, since only the final value ever reaches the image.
    """

    def __init__(self, session, label: str = "auto-tune"):
        self.session = session
        self.label = label
        self._writes: Dict[Tuple[int, int, int], PlannedWrite] = {}
        self._order: List[Tuple[int, int, int]] = []
        self.problems: List[Problem] = []
        self._validated = False

    # -- building ---------------------------------------------------------
    def set(self, target, row: int, col: int, value, reason: str = "") -> None:
        """Intend to write ``value`` into one cell.

        The value recorded is what the map would actually store -- clamped to
        its element width and signedness -- so the plan never claims a write it
        cannot perform. `validate` reports the clamp separately.
        """
        landed = target.clamp(value)
        write = PlannedWrite(target, row, col, target.cell(row, col), landed,
                             reason, requested=value)
        key = write.key
        if key not in self._writes:
            self._order.append(key)
        self._writes[key] = write
        self._validated = False

    def set_many(self, target, cells: Sequence[Tuple[int, int]], value,
                 reason: str = "") -> None:
        for row, col in cells:
            self.set(target, row, col, value, reason)

    def scale(self, target, cells: Sequence[Tuple[int, int]], factor: float,
              reason: str = "") -> None:
        for row, col in cells:
            self.set(target, row, col, target.cell(row, col) * factor, reason)

    def raise_to_at_least(self, target, cells, floor, reason: str = "") -> None:
        """Raise cells that are below ``floor``, leaving the rest alone."""
        for row, col in cells:
            current = target.cell(row, col)
            if current < floor:
                self.set(target, row, col, floor, reason)

    # -- reading ----------------------------------------------------------
    @property
    def writes(self) -> List[PlannedWrite]:
        return [self._writes[k] for k in self._order]

    @property
    def effective_writes(self) -> List[PlannedWrite]:
        """Writes that would actually change something."""
        return [w for w in self.writes if w.changes]

    def value_at(self, target, row: int, col: int):
        """The value this cell would hold after the plan is applied."""
        write = self._writes.get((id(target), row, col))
        return write.new if write is not None else target.cell(row, col)

    def touches(self, target, row: int, col: int) -> bool:
        return (id(target), row, col) in self._writes

    def targets(self) -> List:
        """Every map the plan writes to, in the order first written."""
        seen, out = set(), []
        for w in self.writes:
            if id(w.target) not in seen:
                seen.add(id(w.target))
                out.append(w.target)
        return out

    def __len__(self):
        return len(self._order)

    def __bool__(self):
        return bool(self.effective_writes)

    # -- validation -------------------------------------------------------
    def validate(self, checks=None) -> List[Problem]:
        """Run the safety checks and cache the result on the plan."""
        from .validate import run_checks

        self.problems = run_checks(self, checks)
        self.problems.sort(key=lambda p: (_RANK.get(p.severity, 9), p.code))
        self._validated = True
        return self.problems

    @property
    def errors(self) -> List[Problem]:
        return [p for p in self.problems if p.severity == ERROR]

    @property
    def warnings(self) -> List[Problem]:
        return [p for p in self.problems if p.severity == WARNING]

    @property
    def ok(self) -> bool:
        """True when the plan has been validated and nothing blocks it."""
        return self._validated and not self.errors

    def add_problem(self, severity, code, message, where="") -> None:
        self.problems.append(Problem(severity, code, message, where))

    # -- applying ---------------------------------------------------------
    def apply(self, force: bool = False):
        """Write the plan as one undoable command.

        Refuses unless `validate` has run and found no errors. ``force``
        exists for the one legitimate case -- a human has read the errors and
        decided -- and is never set by a profile.
        """
        if not self._validated:
            raise RuntimeError("validate() the plan before applying it")
        if self.errors and not force:
            raise ValueError(
                f"plan has {len(self.errors)} error(s); refusing to apply: "
                + "; ".join(p.message for p in self.errors[:3]))

        by_target: Dict[int, Tuple[object, List[Tuple[int, int, float]]]] = {}
        for w in self.effective_writes:
            slot = by_target.setdefault(id(w.target), (w.target, []))
            slot[1].append((w.row, w.col, w.new))
        if not by_target:
            return None
        # One transaction over every map the plan touches, so a nine-map tune
        # is one undo step -- the session's edit_cells is per-entity, and
        # nesting them in a transaction is what makes it atomic.
        with self.session.transaction(self.label):
            for target, cells in by_target.values():
                self.session.edit_cells(target, cells)
        return len(self.effective_writes)

    # -- reporting --------------------------------------------------------
    def describe(self, limit: int = 20) -> str:
        lines = [f"{self.label}: {len(self.effective_writes)} cell(s) across "
                 f"{len(self.targets())} map(s)"]
        for w in self.effective_writes[:limit]:
            lines.append("  " + str(w))
        extra = len(self.effective_writes) - limit
        if extra > 0:
            lines.append(f"  ... {extra} more")
        if self.problems:
            lines.append("")
            for p in self.problems[:limit]:
                lines.append("  " + str(p))
        return "\n".join(lines)

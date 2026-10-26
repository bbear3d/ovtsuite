# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Data types of the logging-block planner. No Qt, no ECU knowledge."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: A layout slot is one of: a signal name (a populated cell; "" when nothing
#: names it), EMPTY (an existing slot holding the empty id -- it may be
#: filled) or NO_SLOT (a position the group cannot carry, e.g. an EDC16 group
#: whose +127 row is missing).
EMPTY = None
NO_SLOT = False

#: Kinds of cell write
NEW = "new"            # a cell of a group that was entirely unused
FILL = "fill"          # an empty cell of a group that already carries cells

#: (formula byte, A byte) the ECU sends with a cell; A is None when it is not
#: compared (text formulas send their length there, not the norm).
Fingerprint = Tuple[int, Optional[int]]


@dataclass(frozen=True)
class Requirement:
    """One thing a preset logs.

    ``key`` is a role key ("rpm") for a shipped recipe, or the exact signal for
    a cell of your own preset. ``signals`` are the signal names that satisfy
    it, preferred first. ``pin`` is set for a cell of your preset that nothing
    names: it cannot be moved, so its (group, field) stays in the plan as it
    is."""
    key: str
    signals: Tuple[str, ...] = ()
    need: bool = True
    label: str = ""
    pin: Optional[Tuple[int, int]] = None

    def satisfied_by(self, signal) -> bool:
        return isinstance(signal, str) and bool(signal) and signal in self.signals


@dataclass(frozen=True)
class CellSource:
    """What can be written into a slot to show a signal.

    ``value`` is what the provider writes: an ESB index on EDC16, a tuple of
    ``(dataset, descriptor index)`` pairs on EDC15 (hashable either way).
    ``proven`` is True when a stock cell already shows exactly this source --
    the planner uses only those by default, so every cell decodes as today."""
    value: Any
    signal: str
    fingerprint: Fingerprint
    label: str = ""
    unit: str = ""
    proven: bool = True


@dataclass(frozen=True)
class CellWrite:
    group: int
    slot: int                  # 0-based position in the group
    source: CellSource
    kind: str                  # NEW / FILL
    req: str                   # the requirement it serves


@dataclass
class Layout:
    """The planner's snapshot of a binary's groups.

    ``cells`` holds every group that carries at least one cell; ``free`` the
    groups that carry nothing and may be created (preference order, best
    first). ``reserved`` groups are never created nor filled -- the ECU's
    basic-setting and tester-routine tables name them -- but a populated one
    may still be read as it is. ``unserved`` groups are not used at all (the
    ECU did not answer them as expected on this connection).
    ``reserved_unknown``: the provider could not tell which groups are
    reserved, so every group is (``reserved`` holds them all, ``free`` is
    empty) -- groups are read as they are, never created nor filled."""
    slots: int
    cells: Dict[int, List[Any]] = field(default_factory=dict)
    free: List[int] = field(default_factory=list)
    reserved: set = field(default_factory=set)
    unserved: set = field(default_factory=set)
    reserved_unknown: bool = False

    def copy(self) -> "Layout":
        return Layout(self.slots, {g: list(v) for g, v in self.cells.items()},
                      list(self.free), set(self.reserved), set(self.unserved),
                      self.reserved_unknown)

    def usable(self, group: int) -> bool:
        return group in self.cells and group not in self.unserved

    def fillable(self, group: int) -> List[int]:
        """Empty slots of a populated group that may be written."""
        if group in self.reserved or group in self.unserved:
            return []
        return [s for s, e in enumerate(self.cells.get(group, ())) if e is EMPTY]

    def signals(self, group: int) -> List[str]:
        return [e for e in self.cells.get(group, ()) if isinstance(e, str) and e]

    def apply(self, writes: Sequence[CellWrite]) -> None:
        """Lay ``writes`` onto this snapshot (a later preset then sees them
        as cells it can reuse at no cost)."""
        for w in writes:
            row = self.cells.get(w.group)
            if row is None:
                row = self.cells[w.group] = [EMPTY] * self.slots
                if w.group in self.free:
                    self.free.remove(w.group)
            row[w.slot] = w.source.signal


@dataclass(frozen=True)
class PlanItem:
    """A preset to plan: its key (a recipe key, or ``user:<name>``), title,
    requirements and the groups it reads today (the baseline)."""
    key: str
    title: str
    reqs: Tuple[Requirement, ...]
    baseline: Tuple[int, ...]


@dataclass
class PresetPlan:
    key: str
    title: str
    groups: List[int]                  # read per row, ascending
    baseline: List[int]                # read today
    writes: List[CellWrite]            # this preset's own writes
    covered: List[str]                 # requirement keys the groups carry
    extra: List[str] = field(default_factory=list)   # wants added by free fill

    @property
    def gain(self) -> float:
        return len(self.baseline) / max(1, len(self.groups))

    @property
    def new_groups(self) -> List[int]:
        return sorted({w.group for w in self.writes if w.kind == NEW})

    @property
    def filled_groups(self) -> List[int]:
        return sorted({w.group for w in self.writes if w.kind == FILL})

    def describe(self) -> str:
        bits = []
        if self.filled_groups:
            n = sum(1 for w in self.writes if w.kind == FILL)
            bits.append("fills %d cell%s in %s" % (n, "" if n == 1 else "s",
                                                   ", ".join(map(str, self.filled_groups))))
        if self.new_groups:
            bits.append("new %s" % ", ".join(map(str, self.new_groups)))
        reused = [g for g in self.groups
                  if g not in self.baseline and g not in self.new_groups
                  and g not in self.filled_groups]
        if reused:
            bits.append("uses %s" % ", ".join(map(str, reused)))
        return "; ".join(bits) or "existing blocks only"


@dataclass
class NoGain:
    """A preset that cannot be read in fewer groups than today."""
    key: str
    title: str
    baseline: List[int]
    reason: str


@dataclass
class Allocation:
    plans: List[PresetPlan] = field(default_factory=list)
    skipped: List[NoGain] = field(default_factory=list)

    @property
    def writes(self) -> List[CellWrite]:
        """Every write, once (later presets reuse earlier cells, never
        rewrite them)."""
        out, seen = [], set()
        for p in self.plans:
            for w in p.writes:
                if (w.group, w.slot) not in seen:
                    seen.add((w.group, w.slot))
                    out.append(w)
        return out

    def plan(self, key: str) -> Optional[PresetPlan]:
        return next((p for p in self.plans if p.key == key), None)


__all__ = ["EMPTY", "NO_SLOT", "NEW", "FILL", "Fingerprint", "Requirement",
           "CellSource", "CellWrite", "Layout", "PlanItem", "PresetPlan",
           "NoGain", "Allocation"]

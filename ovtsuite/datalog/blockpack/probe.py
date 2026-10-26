# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Does the ECU serve the optimised groups as they were built?

The logger reads each planned group once (``DiagSession.read_group``) and
hands the cells here. A recorded (written) cell passes when the ECU sends the
formula byte -- and, unless it is a text formula, the A byte -- recorded for
it: that is the ESB formula / norm on EDC16 and the descriptor's display /
norm bytes on EDC15, read straight off the wire, not inferred from values.

"Not there" takes these forms (docs/LOGGING_BLOCKS_PLAN.md, Phase 0):

* a negative response -- ``read_group`` gives None (KWP2000 ``7F 21 31``,
  a KW1281 NAK);
* a record of nothing but padding -- ``parse_cells`` strips trailing
  ``25 00 00`` cells, so a KWP2000 empty group gives ``[]``;
* the platform's empty cell where a signal was written: ``25 00 00`` on
  KWP2000, ``11 20 20`` on KW1281 (an EDC15 empty block still answers four
  cells, so the cell count alone proves nothing there).

A group the plan reads as it is (no recorded cell) only has to answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

Fingerprint = Tuple[int, Optional[int]]


@dataclass(frozen=True)
class GroupExpectation:
    group: int
    #: (slot, fingerprint) of each recorded cell; empty: it only has to answer
    cells: Tuple[Tuple[int, Fingerprint], ...] = ()


@dataclass
class ProbeResult:
    ok: bool
    failed: Dict[int, str] = field(default_factory=dict)    # group -> why
    checked: List[int] = field(default_factory=list)

    def describe(self) -> str:
        if self.ok:
            return "Blocks %s answer as built." % ", ".join(map(str, self.checked))
        return "; ".join("block %d: %s" % (g, why) for g, why in sorted(self.failed.items()))


def expectations(manifest, groups: Sequence[int]) -> List[GroupExpectation]:
    """What each of ``groups`` must answer, from the manifest's records."""
    out = []
    for g in sorted({int(x) for x in groups}):
        cells = tuple(sorted((c.slot, c.fingerprint) for c in manifest.cells_in([g])))
        out.append(GroupExpectation(g, cells))
    return out


def _fid_a(cell) -> Tuple[int, int]:
    if hasattr(cell, "fid"):
        return int(cell.fid), int(cell.a)
    return int(cell[0]), int(cell[1])


def check_group(cells, exp: GroupExpectation, empty_cell: Sequence[int]) -> Optional[str]:
    """Why ``cells`` (what ``read_group`` gave) fails ``exp`` -- None when it
    passes."""
    if cells is None:
        return "no answer (the ECU refused the group)"
    if len(cells) == 0:
        return "empty (padding only)"
    e_fid, e_a = int(empty_cell[0]), int(empty_cell[1])
    for slot, (fid, a) in exp.cells:
        if slot >= len(cells):
            return "cell %d missing" % (slot + 1)
        got_fid, got_a = _fid_a(cells[slot])
        if (got_fid, got_a) == (e_fid, e_a):
            return "cell %d is empty" % (slot + 1)
        if got_fid != fid:
            return "cell %d has formula %02X, built with %02X" % (slot + 1, got_fid, fid)
        if a is not None and got_a != a:
            return "cell %d has scaling byte %02X, built with %02X" % (slot + 1, got_a, a)
    return None


def check(read: Mapping[int, Any], expected: Sequence[GroupExpectation],
          empty_cell: Sequence[int]) -> ProbeResult:
    """Judge every expected group against what was read (a group missing
    from ``read`` counts as not answering)."""
    res = ProbeResult(True)
    for exp in expected:
        res.checked.append(exp.group)
        why = check_group(read.get(exp.group), exp, empty_cell)
        if why:
            res.failed[exp.group] = why
    res.ok = not res.failed
    return res


def unserved(res: ProbeResult, read: Mapping[int, Any], manifest) -> List[int]:
    """The failed groups the usual (stock) blocks must not use either: those
    that did not answer at all (refused, or padding only) and those the plan
    read as they are. A group that answered but without the cells built in it
    -- a stock group filled in the file, on an ECU that still has the old
    software -- still serves its own cells, which the fallback needs."""
    out = []
    for g in sorted(res.failed):
        cells = read.get(g)
        if not cells or not manifest.cells_in([g]):
            out.append(g)
    return out


__all__ = ["GroupExpectation", "ProbeResult", "expectations", "check_group", "check",
           "unserved"]

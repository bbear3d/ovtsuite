# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Advisory checks on an edit, each with an optional auto-fix.

A check never blocks: it reports, and the user decides. When its auto-fix is
switched on, the fix may only take back part of THIS edit -- it moves an
edited cell back toward where it started and never past it -- so a fix can
not invent a change nobody asked for. (The one exception is the monotonic
check's ``"raise"`` mode, the running maximum the 6HP19 retune used, which by
design lifts later rows; it is opt-in.)

Each check judges only what the edit made worse: a calibration that already
breaks a rule in places (the factory 6HP19 has upshift dips of up to 60 rpm)
is not reported for what it shipped with.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .model import ScheduleSet

Grid = Dict[int, List[float]]
Key = Tuple[int, int, int]           # (schedule index, row, column)


@dataclass
class Finding:
    severity: str                    # "warning" | "note"
    code: str
    message: str
    cells: List[Key] = field(default_factory=list)
    fixed: bool = False

    def __str__(self):
        tag = " (auto-fixed)" if self.fixed else ""
        return "%s: %s%s" % (self.severity.upper(), self.message, tag)


def _is_float(s) -> bool:
    return bool(getattr(s.entity, "is_float", False))


def _where(sset, i, r, c) -> str:
    s = sset.schedules[i]
    p = s.pedal[r] if r < len(s.pedal) else r
    return "%s %s @ %.1f %%" % (s.key, sset.line(c).label, p)


class Check:
    code = "check"
    autofix = False

    def run(self, sset: ScheduleSet, origin: Grid, grid: Grid,
            changed: Dict[Key, str]) -> List[Finding]:
        raise NotImplementedError


@dataclass
class GapCheck(Check):
    """An upshift and the downshift that undoes it, at the same pedal, must
    stay apart -- or the box hunts.

    ``min_gap`` is in output rpm. ``relative`` "own" requires
    ``min(the cell's own gap before the edit, min_gap)``: the factory gap is
    per cell and can be far below any fixed number (64 rpm on the 6HP19), so
    an edit is held to what that cell already had, capped at ``min_gap``.
    "fixed" requires ``min_gap`` everywhere the edit touched.

    ``fix`` says which side the auto-fix moves:

    * ``"take_back"`` -- the edited line goes back toward where it started,
      never past it (the 6HP19 downshift passes: a raised 5>4 was capped
      under its 4>5);
    * ``"push_partner"`` -- the edit stands and the paired line moves away by
      the minimum needed (the 6HP19 upshift retune: a lowered 4>5 pushed its
      5>4 down). This changes a line the user did not edit, by design.
    """
    min_gap: float = 60.0
    relative: str = "own"
    autofix: bool = False
    fix: str = "take_back"
    #: Shortfall (output rpm) not worth reporting -- 1 absorbs the rounding of
    #: a moved cell next to a factory gap that is already at its limit.
    tolerance: float = 0.0
    code = "gap"

    def run(self, sset, origin, grid, changed):
        out = []
        touched = {(i, r, c) for (i, r, c) in changed}
        seen = set()
        for (i, r, c) in sorted(touched):
            ln = sset.line(c)
            if ln.pair is None:
                continue
            up, dn = (c, ln.pair) if ln.kind == "up" else (ln.pair, c)
            if (i, r, up) in seen:
                continue
            seen.add((i, r, up))
            s = sset.schedules[i]
            k_up, k_dn = r * s.cols + up, r * s.cols + dn
            if s.is_off(grid[i][k_up]) or s.is_off(grid[i][k_dn]):
                continue
            was = origin[i][k_up] - origin[i][k_dn]
            need = min(was, self.min_gap) if self.relative == "own" else self.min_gap
            now = grid[i][k_up] - grid[i][k_dn]
            if now >= need - 1e-9 - self.tolerance or now >= was:
                continue
            f = Finding("warning", self.code,
                        "%s: gap %s \u2192 %s would be %g rpm out (was %g, keep \u2265 %g)" % (
                            _where(sset, i, r, up), sset.line(up).label,
                            sset.line(dn).label, now, was, need),
                        [(i, r, up), (i, r, dn)])
            if self.autofix and self.fix == "push_partner":
                dn_edited = grid[i][k_dn] != origin[i][k_dn]
                up_edited = grid[i][k_up] != origin[i][k_up]
                if up_edited and not dn_edited:
                    grid[i][k_dn] = min(grid[i][k_dn], grid[i][k_up] - need)
                    if not _is_float(s):
                        grid[i][k_dn] = float(round(grid[i][k_dn]))
                elif dn_edited and not up_edited:
                    grid[i][k_up] = max(grid[i][k_up], grid[i][k_dn] + need)
                    if not _is_float(s):
                        grid[i][k_up] = float(round(grid[i][k_up]))
                f.fixed = grid[i][k_up] - grid[i][k_dn] >= need - 1e-9
                f.cells = [(i, r, up), (i, r, dn)]
            elif self.autofix:
                # Take back the edited side: the downshift first.
                if grid[i][k_dn] != origin[i][k_dn]:
                    v = grid[i][k_up] - need
                    lo, hi = sorted((origin[i][k_dn], grid[i][k_dn]))
                    grid[i][k_dn] = max(lo, min(hi, v))
                if grid[i][k_up] - grid[i][k_dn] < need - 1e-9 \
                        and grid[i][k_up] != origin[i][k_up]:
                    v = grid[i][k_dn] + need
                    lo, hi = sorted((origin[i][k_up], grid[i][k_up]))
                    grid[i][k_up] = max(lo, min(hi, v))
                f.fixed = grid[i][k_up] - grid[i][k_dn] >= need - 1e-9
            out.append(f)
        return out


@dataclass
class MonotonicCheck(Check):
    """A line must not fall as pedal rises (more pedal never means an earlier
    upshift, nor a downshift needing more road speed to happen).

    Fix modes: ``"pull_back"`` lowers the offending earlier row toward the
    next row, never below where it started (downshift pass 2's repair);
    ``"raise"`` is the running maximum over the whole line, which lifts later
    rows (the 6HP19 upshift retune).

    ``whole_line`` also reports and fixes falls the calibration already had in
    the lines this edit touched -- the retune flattened factory droops on
    purpose ("more revs at high pedal too"). Off by default: a fall the factory
    shipped is not this edit's doing.
    """
    autofix: bool = False
    mode: str = "pull_back"
    tolerance: float = 0.0
    whole_line: bool = False
    #: Lines to check besides the ones the edit changed: ``(schedules, lines)``
    #: index lists. The retune ran its running maximum over every schedule's
    #: upshift columns, edited or not.
    scope: Optional[Tuple[Sequence[int], Sequence[int]]] = None
    code = "monotonic"

    def run(self, sset, origin, grid, changed):
        out = []
        pairs = {(i, c) for (i, r, c) in changed}
        if self.scope is not None:
            pairs |= {(i, c) for i in self.scope[0] for c in self.scope[1]}
        for i, c in sorted(pairs):
            s = sset.schedules[i]
            K = s.cols
            col = lambda g: [g[i][r * K + c] for r in range(s.rows)]
            now, was = col(grid), col(origin)
            bad = [r for r in range(s.rows - 1)
                   if now[r] > now[r + 1] + self.tolerance
                   and (self.whole_line or not was[r] > was[r + 1] + self.tolerance)]
            if not bad:
                continue
            f = Finding("warning", self.code,
                        "%s %s falls as pedal rises at %s" % (
                            s.key, sset.line(c).label,
                            ", ".join("%.1f %%" % s.pedal[r] for r in bad)),
                        [(i, r, c) for r in bad])
            if self.autofix:
                if self.mode == "raise":
                    run = None
                    for r in range(s.rows):
                        v = grid[i][r * K + c]
                        if run is not None and v < run:
                            grid[i][r * K + c] = run
                        run = max(run, grid[i][r * K + c]) if run is not None else v
                else:
                    for r in range(s.rows - 2, -1, -1):
                        a, b = grid[i][r * K + c], grid[i][(r + 1) * K + c]
                        if a > b + self.tolerance:
                            grid[i][r * K + c] = max(origin[i][r * K + c], b)
                now = col(grid)
                f.fixed = not any(now[r] > now[r + 1] + self.tolerance
                                  and (self.whole_line
                                       or not was[r] > was[r + 1] + self.tolerance)
                                  for r in range(s.rows - 1))
            out.append(f)
        return out


@dataclass
class RpmLimitCheck(Check):
    """Engine rpm after a shift within ``[floor, ceiling]``.

    An upshift lands in the gear it enters (floor = no lugging); a downshift
    lands in the lower gear it enters (ceiling = no over-revving). Only
    edited cells are judged, so the factory's own extremes are not reported.
    """
    floor: Optional[float] = None
    ceiling: Optional[float] = None
    autofix: bool = False
    code = "rpm"

    def run(self, sset, origin, grid, changed):
        out = []
        for (i, r, c) in sorted(changed):
            s, ln = sset.schedules[i], sset.line(c)
            v = grid[i][r * s.cols + c]
            if s.is_off(v):
                continue
            ratio = s.ratio or ln.ratio_entered or 1.0
            after = v * ratio
            msg = None
            if self.floor is not None and after < self.floor:
                msg, lim = "lands at %d rpm, below %d" % (after, self.floor), self.floor
            elif self.ceiling is not None and after > self.ceiling:
                msg, lim = "lands at %d rpm, above %d" % (after, self.ceiling), self.ceiling
            if msg is None:
                continue
            f = Finding("warning", self.code, "%s %s" % (_where(sset, i, r, c), msg),
                        [(i, r, c)])
            if self.autofix:
                target = lim / ratio
                o = origin[i][r * s.cols + c]
                lo, hi = sorted((o, v))
                grid[i][r * s.cols + c] = float(round(max(lo, min(hi, target))))
                f.fixed = True
            out.append(f)
        return out


@dataclass
class SharedCheck(Check):
    """Say when an edited schedule also serves other selection states: the
    edit reaches those programs too (SKL 16 is also state 6 on the 6HP19)."""
    code = "shared"

    def run(self, sset, origin, grid, changed):
        out = []
        for i in sorted({i for (i, r, c) in changed}):
            s = sset.schedules[i]
            if len(s.states) > 1:
                what = "programs SKL" if sset.kind == "tcc" else "states"
                out.append(Finding("note", self.code,
                                   "%s is used by %s %s -- the edit reaches all of them"
                                   % (s.key, what, ", ".join(map(str, s.states)))))
        return out


@dataclass
class ReferenceGapCheck(Check):
    """A line must stay at least ``margin`` below its reference curves -- for
    lock-up, "full lock (RC) completes before the upshift out of the gear".

    ``line`` is the line judged (a Line.index); ``refs`` the reference names to
    use, or None for every program the schedule serves (``"SKL n"`` for each
    of its ``states``). Only edited cells are judged.
    """
    line: int = 2
    margin: float = 0.0
    refs: Optional[Sequence[str]] = None
    code = "reference"

    def run(self, sset, origin, grid, changed):
        from .model import interp
        out = []
        for (i, r, c) in sorted(changed):
            if c != self.line:
                continue
            s = sset.schedules[i]
            v = grid[i][r * s.cols + c]
            if s.is_off(v):
                continue
            names = self.refs if self.refs is not None else ["SKL %d" % n for n in s.states]
            p = s.pedal[r]
            close = []
            for nm in names:
                got = s.refs.get(nm)
                if got and interp(got[0], got[1], p) - v < self.margin:
                    close.append("%s (%.0f)" % (nm, interp(got[0], got[1], p)))
            if close:
                out.append(Finding("warning", self.code,
                                   "%s: %s %.0f is within %g rpm of the upshift in %s"
                                   % (_where(sset, i, r, c), sset.line(c).label, v,
                                      self.margin, ", ".join(close[:6])
                                      + (" ..." if len(close) > 6 else "")),
                                   [(i, r, c)]))
        return out


@dataclass
class IdenticalCheck(Check):
    """Schedules that were byte-identical before the edit and are not after.

    The factory keeps some pairs identical on purpose -- on the 6HP19 the
    lock-up characteristics in slots 2/6, 3/7 and 4/8 of every map, and
    groups of shift programs such as SKL 29/35/36/37. An edit that reaches
    only some members of such a group splits it; this says so. The auto-fix
    extends the edit to the members it missed (copying the edited member's
    result), but only where every edited member of the group agrees -- it
    never chooses between two different edits.
    """
    autofix: bool = False
    code = "identical"

    def run(self, sset, origin, grid, changed):
        out = []
        groups: Dict[tuple, List[int]] = {}
        for i, s in enumerate(sset.schedules):
            groups.setdefault((s.bank, tuple(s.pedal), tuple(origin[i])), []).append(i)
        for members in groups.values():
            if len(members) < 2:
                continue
            now = {i: tuple(grid[i]) for i in members}
            if len(set(now.values())) == 1:
                continue
            edited = [i for i in members if list(now[i]) != origin[i]]
            if not edited:
                continue
            missed = [i for i in members if i not in edited]
            names = ", ".join(sset.schedules[i].key for i in members)
            f = Finding("warning", self.code,
                        "%s were identical; this edit makes them differ%s" % (
                            names, (" (not edited: %s)" % ", ".join(
                                sset.schedules[i].key for i in missed)) if missed else ""),
                        [(i, 0, 0) for i in members])
            if self.autofix and missed and len({now[i] for i in edited}) == 1:
                src = list(now[edited[0]])
                for i in missed:
                    grid[i] = list(src)
                f.fixed = True
            out.append(f)
        return out

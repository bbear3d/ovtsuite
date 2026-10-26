# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Fewest groups for a preset: create new groups, fill stock ones, or both.

:func:`optimise` plans one preset against a :class:`~.model.Layout`:

1. The **baseline** is what the preset reads today (given by the caller:
   ``recipe_groups`` / ``userpresets.resolve`` on the stock view). T is
   every requirement those groups carry -- the plan must keep all of it.
2. For k = 1 .. B-1 groups, try every set of stock groups (exhaustively up to
   two besides any pinned ones, three among the most useful, greedy beyond):
   what they leave of T must fit in their empty cells plus new groups, and
   the total must be k. The first k that works wins; at that k: the fewest
   **new** groups (filling a stock group beats creating one), then the fewest
   cells written, then the lowest group numbers.
3. Uncovered requirements go into the chosen groups' empty cells first, then
   into new groups (needs before wants, in the preset's order).
4. **Free fill**: a new group's spare cells take wanted signals the preset
   did not have yet. It never adds a group or touches another stock cell.

If no k below B works, the preset gets a :class:`~.model.NoGain` with the
reason, and nothing may be written for it.

:func:`allocate` plans several presets one after another on a copy of the
layout, largest gain first, so a later preset reuses the cells an earlier one
created (engine speed and quantity end up shared, not duplicated).
"""
from __future__ import annotations

from itertools import combinations
from math import ceil
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .model import (FILL, NEW, Allocation, CellSource, CellWrite, Layout, NoGain,
                    PlanItem, PresetPlan, Requirement)

Picker = Callable[[Requirement], Optional[CellSource]]

#: Stock-group combinations tried exhaustively up to this size (besides pins)
EXHAUSTIVE = 2
#: ...then this many of the most useful groups for one size more
TOP_FOR_NEXT = 30


def source_picker(sources: Callable[[str], Iterable[CellSource]],
                  allow_unproven: bool = False) -> Picker:
    """Requirement -> the source to write for it: its signals in preference
    order, the first proven source of the first signal that has one (any
    source with ``allow_unproven``). Cached per requirement key."""
    cache: Dict[Tuple[str, Tuple[str, ...]], Optional[CellSource]] = {}

    def pick(req: Requirement) -> Optional[CellSource]:
        k = (req.key, req.signals)
        if k not in cache:
            best = None
            for sig in req.signals:
                try:
                    cands = list(sources(sig) or ())
                except Exception:
                    cands = []
                best = next((s for s in cands if s.proven or allow_unproven), None)
                if best is not None:
                    break
            cache[k] = best
        return cache[k]
    return pick


def _carries(layout: Layout, group: int, req: Requirement) -> bool:
    return any(req.satisfied_by(s) for s in layout.cells.get(group, ()))


def _popcount(x: int) -> int:
    return bin(x).count("1")


def optimise(key: str, title: str, reqs: Sequence[Requirement], layout: Layout,
             baseline: Sequence[int], pick: Picker,
             stock: Optional[Layout] = None) -> Union[PresetPlan, NoGain]:
    """The best layout change for one preset, or why there is none.

    ``stock`` is the layout the baseline was measured on, when ``layout``
    already carries other presets' cells (:func:`allocate`): what the preset
    must keep is what its baseline carries THERE, not what earlier presets
    happened to add to those groups."""
    base = sorted({int(g) for g in baseline})
    B = len(base)
    if B == 0:
        return NoGain(key, title, base, "none of its signals is in a block of this file")
    if B == 1:
        return NoGain(key, title, base, "already read in 1 block")
    pins = sorted({r.pin[0] for r in reqs if r.pin})
    for p in pins:
        if not layout.usable(p):
            return NoGain(key, title, base,
                          "block %d (a cell nothing names) is not available" % p)
    ref = stock if stock is not None else layout
    target = [r for r in reqs if not r.pin and any(_carries(ref, g, r) for g in base)]
    if not target and not pins:
        return NoGain(key, title, base, "none of its cells is in a block it reads today")

    full = (1 << len(target)) - 1
    usable = [g for g in sorted(layout.cells) if layout.usable(g)]
    mask = {g: sum(1 << i for i, r in enumerate(target) if _carries(layout, g, r))
            for g in usable}
    fill = {g: len(layout.fillable(g)) for g in usable}
    unsourced = sum(1 << i for i, r in enumerate(target) if pick(r) is None)
    pin_mask = 0
    for p in pins:
        pin_mask |= mask[p]
    pin_fill = sum(fill[p] for p in pins)
    cands = [g for g in usable if g not in pins and (mask[g] or fill[g])]
    rank = sorted(cands, key=lambda g: (-_popcount(mask[g]), -fill[g], g))
    nfree = len(layout.free)

    def combos(size: int) -> Iterable[Tuple[int, ...]]:
        if size == 0:
            return [()]
        if size <= EXHAUSTIVE:
            return combinations(cands, size)
        if size == EXHAUSTIVE + 1:
            return combinations(sorted(rank[:TOP_FOR_NEXT]), size)
        # greedy: the most useful groups, adding the best marginal one each time
        chosen: List[int] = []
        cov = pin_mask
        for _ in range(size):
            pool = [g for g in rank if g not in chosen]
            if not pool:
                break
            g = max(pool, key=lambda x: (_popcount(mask[x] & ~cov & full), fill[x], -x))
            chosen.append(g)
            cov |= mask[g]
        return [tuple(sorted(chosen))] if len(chosen) == size else []

    best = None
    short_of_free = False
    for k in range(max(1, len(pins)), B):
        for extra in range(min(k - len(pins), len(cands)), -1, -1):
            for combo in combos(extra):
                cov = pin_mask
                for g in combo:
                    cov |= mask[g]
                if unsourced & ~cov & full:
                    continue
                left = _popcount(full & ~cov)
                cap = pin_fill + sum(fill[g] for g in combo)
                n_new = ceil(max(0, left - cap) / layout.slots)
                total = len(pins) + extra + n_new
                if total > k:
                    continue
                if n_new > nfree:
                    short_of_free = True
                    continue
                writes = min(left, cap) + max(0, left - cap)
                cost = (total, n_new, writes, tuple(sorted(pins + list(combo))))
                if best is None or cost < best[0]:
                    best = (cost, combo)
        if best is not None:
            break
    if best is None:
        if layout.reserved_unknown:
            why = ("already read in %d blocks; with the blocks as they are it can't be done in "
                   "fewer, and none may be changed (which blocks the ECU keeps for itself is "
                   "unknown)" % B)
        elif unsourced & full and not any(pick(r) for r in target):
            why = ("already read in %d blocks; with the blocks as they are it can't be done in "
                   "fewer, and no cell can be added (no cell definitions to write)" % B)
        else:
            why = ("not enough free blocks" if short_of_free else
                   "already read in %d blocks; it can't be done in fewer" % B)
            if unsourced and not short_of_free:
                why += " (some of its cells have no other source than where they are)"
        return NoGain(key, title, base, why)

    chosen = sorted(pins + list(best[1]))
    cov = pin_mask
    for g in best[1]:
        cov |= mask[g]
    left = [r for i, r in enumerate(target) if not (cov >> i) & 1]
    slots_fill = [(g, s) for g in chosen for s in layout.fillable(g)]
    writes: List[CellWrite] = []
    for (g, s), r in zip(slots_fill, left):
        writes.append(CellWrite(g, s, pick(r), FILL, r.key))
    rest = left[len(slots_fill):]
    n_new = ceil(len(rest) / layout.slots) if rest else 0
    new_groups = list(layout.free[:n_new])
    spare: List[Tuple[int, int]] = []
    for i, g in enumerate(new_groups):
        part = rest[i * layout.slots:(i + 1) * layout.slots]
        for s, r in enumerate(part):
            writes.append(CellWrite(g, s, pick(r), NEW, r.key))
        spare += [(g, s) for s in range(len(part), layout.slots)]

    groups = sorted(chosen + new_groups)
    have = {w.req for w in writes}
    covered = [r.key for r in reqs if not r.pin and
               (r.key in have or any(_carries(layout, g, r) for g in chosen))]
    extra: List[str] = []
    if spare:
        for r in reqs:
            if not spare:
                break
            if r.pin or r.key in covered or r.key in extra:
                continue
            src = pick(r)
            if src is None:
                continue
            g, s = spare.pop(0)
            writes.append(CellWrite(g, s, src, NEW, r.key))
            extra.append(r.key)
    return PresetPlan(key, title, groups, base, writes, covered + extra, extra)


def allocate(items: Sequence[PlanItem], layout: Layout, pick: Picker) -> Allocation:
    """Plan every item. Items that gain are planned first, largest gain first,
    each on the layout the ones before it left -- so cells are shared, and an
    item that gained nothing alone is tried again last, in case the others
    created cells it can use. Plans come back in the items' order."""
    alone = [optimise(it.key, it.title, it.reqs, layout, it.baseline, pick) for it in items]

    def order(i):
        p = alone[i]
        return (isinstance(p, NoGain), -(p.gain if isinstance(p, PresetPlan) else 0), i)

    work = layout.copy()
    done: Dict[int, Union[PresetPlan, NoGain]] = {}
    for i in sorted(range(len(items)), key=order):
        it = items[i]
        p = optimise(it.key, it.title, it.reqs, work, it.baseline, pick, stock=layout)
        if isinstance(p, PresetPlan):
            work.apply(p.writes)
        done[i] = p
    out = Allocation()
    for i in range(len(items)):
        (out.plans if isinstance(done[i], PresetPlan) else out.skipped).append(done[i])
    return out


__all__ = ["Picker", "source_picker", "optimise", "allocate"]

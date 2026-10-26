# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What a platform module supplies so its measuring blocks can be optimised,
how the base app finds it, and the planner's snapshot of a binary.

A module offers a provider through its ``ModuleSpec`` hook
``msblock_layout`` (``msblock_layout(session) -> LayoutProvider``), the way it
offers ``dyno`` or ``injectors``; :func:`register` covers a provider that is
not (yet) behind a hook, and tests. A platform without one simply has no
optimised blocks -- the dialog and the logger hide the feature.

The provider speaks in *positions*: group numbers as the tester asks for
them, slots 0-based in the order the ECU sends cells. On EDC16 slots 0-3 are
the group's own map row and 4-7 its +127 row; on EDC15 slots 0-3 are
``xcwKNN_1..4``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:                                       # Python 3.8+: typing.Protocol
    from typing import Protocol
except ImportError:                        # pragma: no cover
    Protocol = object                      # type: ignore

from .model import EMPTY, NO_SLOT, CellSource, CellWrite, Layout

#: :meth:`LayoutProvider.slot_states` values
USED = "used"
FREE = "empty"
NONE = "none"


@dataclass(frozen=True)
class LayoutIdentity:
    """What the ECU will report for a binary with this calibration -- the
    keys the per-user store files a manifest under (EDC16: part number and
    software version; EDC15: each dataset's ident)."""
    platform: str
    keys: Tuple[str, ...] = ()


class LayoutProvider(Protocol):
    platform: str

    def slots(self) -> int:
        """Cells per group (EDC16 8, EDC15 4)."""

    def group_range(self) -> Sequence[int]:
        """Every group number the tester can ask for and the binary defines."""

    def slot_states(self, group: int) -> List[str]:
        """Per slot: USED (holds a cell), FREE (holds the empty id and may be
        written) or NONE (the group has no such position)."""

    def reserved_groups(self) -> Optional[Set[int]]:
        """Groups never created nor filled (named by the ECU's own
        basic-setting / tester-routine tables). None when they cannot be
        known for this binary (the tables that name them are missing): the
        planner then creates and fills no group at all. An empty set means
        "known: none" (EDC15) -- never use it for "not found"."""

    def notes(self) -> List[str]:
        """Optional: what limits the analysis of this binary (tables the
        definition leaves out, found or not), in words for the window."""

    def free_sort_key(self, group: int) -> Any:
        """Order of preference for new groups (lowest key first). The default
        policy is highest group number first."""

    def sources(self, signal: str) -> List[CellSource]:
        """What can be written to show ``signal``, proven ones first."""

    def value_at(self, group: int, slot: int) -> Any:
        """The value a slot holds now (compared with a manifest's record)."""

    def empty_cell(self) -> Tuple[int, int]:
        """(formula, A) an empty slot is sent as: KWP2000 (0x25, 0x00),
        KW1281 (0x11, 0x20)."""

    def write(self, writes: Sequence[CellWrite], label: str) -> int:
        """Write every cell as ONE undoable step; returns how many changed."""

    def restore(self, cells: Iterable[Tuple[int, int, Any]], label: str) -> int:
        """Put the empty id back in each (group, slot) that still holds the
        given value, as ONE undoable step; returns how many changed."""

    def identity(self) -> LayoutIdentity:
        ...


# ---------------------------------------------------------------- lookup
_REGISTRY: Dict[str, Callable[[Any], Optional[LayoutProvider]]] = {}


def register(platform: str, factory: Callable[[Any], Optional[LayoutProvider]]) -> None:
    """``factory(session) -> provider`` for sessions of ``platform`` (used when
    the platform's module has no ``msblock_layout`` hook)."""
    _REGISTRY[platform] = factory


def unregister(platform: str) -> None:
    _REGISTRY.pop(platform, None)


def provider_for(session) -> Optional[LayoutProvider]:
    """The layout provider for ``session``'s platform, or None."""
    if session is None:
        return None
    platform = getattr(session, "platform", "") or ""
    spec = None
    try:
        from ...modules import registry as _registry
        spec = _registry.for_platform(platform)
    except Exception:
        spec = None
    if spec is not None and getattr(spec, "msblock_layout", None) and \
            getattr(spec, "variant", "full") == "full":
        try:
            p = spec.call("msblock_layout", session)
        except Exception:
            p = None
        if p is not None:
            return p
    factory = _REGISTRY.get(platform)
    if factory is None:
        return None
    try:
        return factory(session)
    except Exception:
        return None


# ---------------------------------------------------------------- snapshot
def build_layout(provider: LayoutProvider, namer=None, manifest=None,
                 release: Optional[Iterable[str]] = None,
                 unserved: Iterable[int] = ()) -> Layout:
    """The planner's view of the binary behind ``provider``.

    Cells are named through ``namer`` (normally a ``StockNamer`` over the
    binary's ``ChannelNamer``); a cell it does not name is still a cell ("").

    With ``manifest`` and ``release`` (preset keys being planned again or
    removed), the recorded cells only those presets read -- and that the
    binary still holds -- count as empty again, and a group left with nothing
    else is free again. Every other recorded cell stays a cell, named from the
    record, so the plan reuses it rather than overwriting it.
    """
    slots = int(provider.slots())
    got = provider.reserved_groups()
    unknown = got is None
    reserved = set() if unknown else set(got)
    rec: Dict[Tuple[int, int], Any] = {}
    released: Set[Tuple[int, int]] = set()
    if manifest is not None:
        valid = manifest.valid_cells(provider.value_at)
        rec = {c.where: c for c in manifest.cells if c.where in valid}
        if release is not None:
            released = {c.where for c in manifest.owned_only_by(release) if c.where in valid}
    cells: Dict[int, List[Any]] = {}
    free: List[int] = []
    for g in provider.group_range():
        states = list(provider.slot_states(g))[:slots]
        states += [NONE] * (slots - len(states))
        row: List[Any] = []
        used = False
        for s, st in enumerate(states):
            if st == NONE:
                row.append(NO_SLOT)
            elif st == FREE or (g, s) in released:
                row.append(EMPTY)
            else:
                used = True
                c = rec.get((g, s))
                if c is not None:
                    row.append(c.signal)
                elif namer is not None:
                    try:
                        row.append(namer.resolve(g, s)[1] or "")
                    except Exception:
                        row.append("")
                else:
                    row.append("")
        if used:
            cells[g] = row
        elif g not in reserved and all(e is EMPTY for e in row):
            free.append(g)
        elif g not in reserved and any(e is EMPTY for e in row):
            cells[g] = row                     # a partial group: fill only
    free.sort(key=provider.free_sort_key)
    if unknown:
        # which groups the ECU keeps for itself is not known: read groups as
        # they are, but create and fill none
        reserved |= set(int(g) for g in provider.group_range())
        free = []
    return Layout(slots, cells, free, reserved, set(int(u) for u in unserved),
                  reserved_unknown=unknown)


__all__ = ["USED", "FREE", "NONE", "LayoutIdentity", "LayoutProvider", "register",
           "unregister", "provider_for", "build_layout"]

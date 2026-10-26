# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Namers over a layout manifest, with the logger's namer interface
(``available``, ``slots``, ``cell_fields``, ``resolve``, ``channel``,
``populated_groups``, ``group_summary``, ``find_signal``) so ``logtune.roles``,
``userpresets`` and ``served`` work on them unchanged.

* :class:`LayoutNamer` lays the manifest's recorded cells over a base namer:
  with no binary open the catalogue does not know them, so their names come
  from the record.
* :class:`StockNamer` hides recorded cells -- the view the baseline and the
  fallback plan use, so "today's blocks" never include the optimised ones.

Both take the set of (group, slot) to act on, normally the manifest's valid
cells (``LayoutManifest.valid_cells``) or, with no binary, all of them once
the probe has confirmed them.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from ..logfile import Channel


def _generic(group: int, field: int) -> Tuple[str, str, str]:
    return ("Group %d / Cell %d" % (group, field + 1), "", "")


class _Wrap:
    def __init__(self, base):
        self.base = base
        self.session = getattr(base, "session", None)

    def _b(self, name, *a, default=None):
        fn = getattr(self.base, name, None) if self.base is not None else None
        if fn is None:
            return default
        try:
            return fn(*a)
        except Exception:
            return default

    def channel(self, group: int, field: int) -> Channel:
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def group_summary(self, group: int) -> str:
        parts = []
        for f in self.cell_fields(group):
            label = self.resolve(group, f)[0]
            if not label.startswith("Group "):
                parts.append(label.split(" (")[0])
        return ", ".join(parts)

    def find_signal(self, name: str) -> Optional[Tuple[int, int]]:
        for g in self.populated_groups():
            for f in self.cell_fields(g):
                if self.resolve(g, f)[1] == name:
                    return g, f
        return None

    def __getattr__(self, name):
        # anything else the logger asks a namer (describe, dataset, ...)
        base = self.__dict__.get("base")
        if base is None:
            raise AttributeError(name)
        return getattr(base, name)


class LayoutNamer(_Wrap):
    """``base`` with the manifest's cells at ``where`` named from the record."""

    def __init__(self, base, manifest, where: Optional[Iterable[Tuple[int, int]]] = None):
        super().__init__(base)
        self.manifest = manifest
        keep = None if where is None else set(where)
        self._rec: Dict[Tuple[int, int], object] = {
            c.where: c for c in manifest.cells if keep is None or c.where in keep}
        self._groups: Dict[int, Set[int]] = {}
        for g, s in self._rec:
            self._groups.setdefault(g, set()).add(s)

    @property
    def available(self) -> bool:
        return bool(self._rec) or bool(getattr(self.base, "available", False))

    def _base_has(self, group: int) -> bool:
        return group in (self._b("populated_groups", default=[]) or [])

    def slots(self, group: int) -> int:
        n = self._b("slots", group, default=4) or 4
        mine = self._groups.get(group)
        return max(n, max(mine) + 1) if mine else n

    def cell_fields(self, group: int) -> List[int]:
        mine = self._groups.get(group, set())
        if not mine:
            return self._b("cell_fields", group, default=list(range(self.slots(group))))
        base = set(self._b("cell_fields", group, default=[]) or []) if self._base_has(group) else set()
        return sorted(base | mine)

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        c = self._rec.get((group, field))
        if c is not None:
            label = c.label or c.signal or _generic(group, field)[0]
            return (label, c.signal, c.unit)
        if group in self._groups and not self._base_has(group):
            return _generic(group, field)
        return self._b("resolve", group, field, default=_generic(group, field))

    def populated_groups(self) -> List[int]:
        return sorted(set(self._b("populated_groups", default=[]) or []) | set(self._groups))

    def recorded(self, group: int, field: int) -> bool:
        return (group, field) in self._rec


class StockNamer(_Wrap):
    """``base`` with the cells at ``hide`` hidden: the factory view."""

    def __init__(self, base, hide: Iterable[Tuple[int, int]]):
        super().__init__(base)
        self._hide = set(hide)

    @property
    def available(self) -> bool:
        return bool(getattr(self.base, "available", False))

    def slots(self, group: int) -> int:
        return self._b("slots", group, default=4) or 4

    def cell_fields(self, group: int) -> List[int]:
        fields = self._b("cell_fields", group, default=list(range(self.slots(group)))) or []
        return [f for f in fields if (group, f) not in self._hide]

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        if (group, field) in self._hide:
            return _generic(group, field)
        return self._b("resolve", group, field, default=_generic(group, field))

    def populated_groups(self) -> List[int]:
        out = []
        for g in self._b("populated_groups", default=[]) or []:
            fields = self._b("cell_fields", g, default=[]) or []
            if any((g, f) not in self._hide for f in fields):
                out.append(g)
        return sorted(out)


__all__ = ["LayoutNamer", "StockNamer"]

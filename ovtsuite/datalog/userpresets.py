# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The data logger's presets of your own: a named set of cells, per controller.

The built-in presets are planned per controller (the ECU box of the logger:
EDC16, EDC15, ZF6HP...) from the signals a calibration's blocks carry. Yours
are kept the same way -- one list per controller -- and remember each cell as
its block and position **and** the signal it carried when it was saved. On a
calibration whose block table puts that signal somewhere else, the cell is
found again by its signal; one that no block carries any more is reported
missing, as the built-ins report a value no served block carries.

Stored as JSON in the settings (``KEY``): ``{controller: [preset, ...]}``.
Nothing here imports Qt: :func:`load` and :func:`save` take the settings
object the logger already uses.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Settings key the presets are kept under
KEY = "datalog/user_presets"

#: Prefix of a preset of yours in the preset box's item data (a built-in's
#: data is its recipe key, which never has a colon)
DATA_PREFIX = "user:"


@dataclass
class Cell:
    """One measuring-block cell: block, position (0-based), and the signal
    and title it had when the preset was saved ("" when nothing named it)."""
    group: int
    field: int
    signal: str = ""
    title: str = ""

    def to_json(self) -> dict:
        d = {"group": int(self.group), "field": int(self.field)}
        if self.signal:
            d["signal"] = self.signal
        if self.title:
            d["title"] = self.title
        return d

    @classmethod
    def from_json(cls, d) -> "Cell":
        return cls(int(d["group"]), int(d["field"]), str(d.get("signal", "") or ""),
                   str(d.get("title", "") or ""))


@dataclass
class UserPreset:
    name: str
    cells: List[Cell] = field(default_factory=list)
    #: log End of injection (calculated) too, when its two cells are there
    eoi: bool = False

    @property
    def data(self) -> str:
        """What the preset box holds for it."""
        return DATA_PREFIX + self.name

    @property
    def tag(self) -> str:
        """What a log file's name gets for it (as a built-in's key)."""
        return slug(self.name)

    def to_json(self) -> dict:
        d = {"name": self.name, "cells": [c.to_json() for c in self.cells]}
        if self.eoi:
            d["eoi"] = True
        return d

    @classmethod
    def from_json(cls, d) -> "UserPreset":
        return cls(str(d["name"]), [Cell.from_json(c) for c in d.get("cells", [])],
                   bool(d.get("eoi", False)))


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-") or "preset"


def is_user_data(data) -> bool:
    return isinstance(data, str) and data.startswith(DATA_PREFIX)


def name_of_data(data) -> str:
    return data[len(DATA_PREFIX):] if is_user_data(data) else ""


# ---------------------------------------------------------------- storage
def _read_all(prefs) -> Dict[str, list]:
    try:
        raw = prefs.value(KEY, "")
        d = json.loads(raw) if raw else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load(prefs, ecu: str) -> List[UserPreset]:
    """Your presets for controller ``ecu``, in the order saved. Damaged
    entries are skipped, never fatal."""
    out = []
    for d in _read_all(prefs).get(ecu or "", []) or []:
        try:
            out.append(UserPreset.from_json(d))
        except Exception:
            continue
    return out


def save(prefs, ecu: str, presets: Sequence[UserPreset]) -> None:
    """Replace the list for ``ecu``; the other controllers' lists are kept."""
    alld = _read_all(prefs)
    if presets:
        alld[ecu or ""] = [p.to_json() for p in presets]
    else:
        alld.pop(ecu or "", None)
    prefs.setValue(KEY, json.dumps(alld))
    try:
        prefs.sync()
    except Exception:
        pass


def find(presets: Sequence[UserPreset], name: str,
         but: str = "") -> Optional[UserPreset]:
    """The preset other than ``but`` whose name is ``name`` (case and
    surrounding spaces ignored): names are unique per controller."""
    low = (name or "").strip().lower()
    skip = (but or "").strip().lower()
    return next((p for p in presets if p.name.strip().lower() == low
                 and p.name.strip().lower() != skip), None)


# ---------------------------------------------------------------- cells
def from_channels(name: str, channels) -> UserPreset:
    """A preset of the logger's selected channels (block cells and the
    calculated End of injection; anything else is not a cell to keep)."""
    cells, seen = [], set()
    eoi = False
    for c in channels:
        if getattr(c, "is_derived", False):
            eoi = eoi or getattr(c, "derived", "") == "eoi"
            continue
        if getattr(c, "is_memory", False):
            continue
        k = (int(c.group), int(c.field))
        if k in seen:
            continue
        seen.add(k)
        cells.append(Cell(k[0], k[1], getattr(c, "signal", "") or "",
                          getattr(c, "title", "") or ""))
    return UserPreset(name.strip(), cells, eoi)


@dataclass
class Resolved:
    cells: List[Tuple[int, int]]        # (group, field) to log, in order
    missing: List[str]                  # titles of cells no served block carries
    moved: int = 0                      # cells found again in another block


def resolve(preset: UserPreset, namer, avoid: Sequence[int] = ()) -> Resolved:
    """Where ``preset``'s cells are on the calibration ``namer`` describes.

    A cell whose block and position still carry its signal is logged there.
    One whose signal is now elsewhere is logged where the namer finds it; one
    no block carries is missing. With no namer that can name the cells (or a
    cell saved unnamed), the block and position are taken as they are --
    except a block in ``avoid`` (blocks the ECU does not serve), which is
    reported missing like a built-in preset's."""
    named = bool(getattr(namer, "available", False))
    finder = getattr(namer, "find_signal", None) if named else None
    out, missing, moved, seen = [], [], 0, set()
    bad = set(int(g) for g in avoid or ())
    for c in preset.cells:
        at: Optional[Tuple[int, int]] = (c.group, c.field)
        if named and c.signal:
            try:
                now = namer.channel(c.group, c.field).signal
            except Exception:
                now = ""
            if now != c.signal:
                hit = None
                if finder is not None:
                    try:
                        hit = finder(c.signal)
                    except Exception:
                        hit = None
                at = (int(hit[0]), int(hit[1])) if hit else None
                if at is not None:
                    moved += 1
        if at is not None and at[0] in bad:
            at = None
        if at is None:
            missing.append(c.title or c.signal or "Group %d / Cell %d" % (c.group, c.field + 1))
            continue
        if at not in seen:
            seen.add(at)
            out.append(at)
    return Resolved(out, missing, moved)


__all__ = ["KEY", "Cell", "UserPreset", "Resolved", "load", "save", "find",
           "from_channels", "resolve", "slug", "is_user_data", "name_of_data"]

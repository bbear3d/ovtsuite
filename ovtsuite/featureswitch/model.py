# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Feature switches: what a feature bundle is, and where the bundles come from.

A *feature* is a bundle of calibration parameters changed together (A/C,
cruise, the CAN partners of a standalone chassis, ...), not a single switch.
Each parameter carries the value it takes in each target *state* and a
*provenance* saying how that value is known. The engine (:mod:`.engine`)
resolves each parameter name to the entity (or per-dataset / per-coding
copies) in the loaded file, reads the current state, and -- for the copies
the user ticks -- writes the target state as one undo step.

The bundles themselves -- which parameters, which values -- belong to the
platform's module: its ``features`` hook (``ModuleSpec.features``) returns a
:class:`FeatureSet`, the bundles plus the *table hooks* a bundle names for
the edits that are more than a switch (see :data:`HOOK_PROTOCOL`). Nothing
here guesses an address or invents a value: a parameter is located by the
loaded definition, and a value with provenance ``USER`` or ``DERIVED`` is
filled by the user or computed from the file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# -- provenance of a target value -----------------------------------------
CODE = "code"        # confirmed in the disassembly
A2L = "a2l"          # the meaning is stated in the A2L / DAMOS text
STOCK = "stock"      # consistent across stock files of each state
DERIVED = "derived"  # computed from this file's own calibration
USER = "user"        # not derivable; the user supplies it

#: a parameter whose value a bundle sets (or shows, when every state is None)
@dataclass(frozen=True)
class Param:
    name: str                       # the A2L / DAMOS parameter name
    meaning: str                    # one line, for the UI
    prov: str = STOCK               # provenance of the values below
    values: Dict[str, Optional[float]] = field(default_factory=dict)
    bits: int = 8                   # 8 for a codeword / Val_C, 16 for a word
    eeprom: Optional[str] = None    # EEPROM field id when the value can live
                                    # in the EEPROM switch record (EDC16, §7.2)
    optional: bool = False          # absent on some files of the family -> skip
    readonly: bool = False          # shown, never written (e.g. gearbox type)

    def target(self, state: str) -> Optional[float]:
        """The value this parameter takes in ``state`` (None = leave as is)."""
        return self.values.get(state)


@dataclass(frozen=True)
class Bundle:
    id: str
    title: str
    platform: str                   # "edc15" | "edc16"
    states: List[str]               # ordered target states for the UI
    params: List[Param]
    families: Optional[Tuple[str, ...]] = None   # restrict to these KB families
    summary: str = ""
    notes: Tuple[str, ...] = ()     # cautions shown in the UI
    table_hook: Optional[str] = None  # engine hook for a table precondition
    readonly: bool = False          # the whole bundle is informational

    def param(self, name: str) -> Optional[Param]:
        return next((p for p in self.params if p.name == name), None)


@dataclass
class FeatureSet:
    """What a module's ``features`` hook returns."""
    bundles: List[Bundle] = field(default_factory=list)
    #: ``Bundle.table_hook`` name -> an object (usually a module) with the
    #: functions of :data:`HOOK_PROTOCOL`
    hooks: Dict[str, object] = field(default_factory=dict)


#: A table hook provides ``present(session)``, ``groups(session)``,
#: ``state(session, group)``, ``fs_describe(session, group, target, **opts)``
#: (one line for the window) and ``fs_apply(session, group, target, label,
#: first, **opts) -> int`` (the writes, inside the caller's transaction;
#: ``first`` is True for the first copy written, for the file-wide part such
#: as fault paths). An optional ``FS_OPTION`` dict adds one number to the
#: window: ``key`` (the keyword passed in ``opts``), ``label``, ``min``,
#: ``max``, ``default``, ``step``, ``suffix``, ``tip``.
HOOK_PROTOCOL = ("present", "groups", "state", "fs_describe", "fs_apply")

_EMPTY = FeatureSet()


def feature_set(session) -> FeatureSet:
    """The installed module's features for ``session``'s platform (an empty
    set without a module, or with only its checksum variant)."""
    pid = getattr(session, "platform", "") or ""
    try:
        from ..modules import registry as _registry
        m = _registry.for_platform(pid)
    except Exception:
        return _EMPTY
    if m is None or not getattr(m, "features", None) or             getattr(m, "variant", "full") != "full":
        return _EMPTY
    cached = _CACHE.get((m.id, m.version))
    if cached is None:
        try:
            cached = m.call("features") or _EMPTY
        except Exception:
            cached = _EMPTY
        _CACHE[(m.id, m.version)] = cached
    return cached


_CACHE: Dict[tuple, FeatureSet] = {}


def bundles_for(session) -> List[Bundle]:
    """The bundles for ``session``'s platform."""
    pid = getattr(session, "platform", "") or ""
    return [b for b in feature_set(session).bundles if b.platform == pid]


def hook(session, name: Optional[str]):
    """The table hook ``name`` for ``session``'s platform, or None."""
    if not name:
        return None
    return feature_set(session).hooks.get(name)


__all__ = [
    "Param", "Bundle", "FeatureSet", "feature_set", "bundles_for", "hook",
    "CODE", "A2L", "STOCK", "DERIVED", "USER", "HOOK_PROTOCOL",
]

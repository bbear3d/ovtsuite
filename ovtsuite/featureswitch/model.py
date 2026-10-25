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
copies: an EDC16 coding-variant copy is one more copy, see
:mod:`.ents`) in the loaded file, reads the current state, and -- for the
copies the user picks -- writes the target state as one undo step.

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
    readonly: bool = False          # shown, never written
    #: what each stored value means, for the value picker ({0: "Off", ...});
    #: empty: any number in the parameter's range
    choices: Dict[int, str] = field(default_factory=dict)

    def target(self, state: str) -> Optional[float]:
        """The value this parameter takes in ``state`` (None = leave as is)."""
        return self.values.get(state)

    def value_text(self, v: Optional[int]) -> str:
        """``v`` as the window shows it: "1 (On)", or the number."""
        if v is None:
            return ""
        lab = self.choices.get(int(v))
        return "%d (%s)" % (v, lab) if lab else str(v)


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
    #: how each state is named in the window ({"can": "CAN"}); a state not
    #: listed is shown with its first letter capitalised
    state_labels: Dict[str, str] = field(default_factory=dict)

    def param(self, name: str) -> Optional[Param]:
        return next((p for p in self.params if p.name == name), None)

    def label(self, state: Optional[str]) -> str:
        if not state:
            return ""
        lab = self.state_labels.get(state)
        if lab:
            return lab
        t = state.replace("_", " ")
        return t[:1].upper() + t[1:]


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
#: ``max``, ``default``, ``step``, ``suffix``, ``tip``, and ``states`` (the
#: settings it applies to; all when absent). An optional ``FS_CHECK`` dict
#: adds a tick box: ``key`` (passed in ``opts`` as True / False), ``label``,
#: ``default``, ``tip`` and ``states``; ``fs_check_why(session, group,
#: target, **opts)`` says why it can't apply to a copy ("" when it can), and
#: the window greys it with that reason. ``fs_preview(session, group,
#: target, first, **opts)`` lists what a setting changes (rows of
#: ``engine.write_rows`` / ``engine.fault_rows``) and ``state_text(session,
#: group)`` says in words what the file holds when no setting matches.
#: ``fs_involved(session, group, ref=None, **opts)`` lists the tables and
#: fault paths the feature works through, with what they hold now, for the
#: window before a setting is picked (rows as ``fs_preview``'s, ``new`` "").
#: It should name every table, axis and value the hook can write, so the
#: window lists them before a setting is picked. A table row carries its
#: ``entity`` (and ``axis``, ``cells``); a fault row its path's ``key`` and
#: ``target`` class, which the window lets the user override path by path.
#: ``fs_absent_why(session)`` (optional) says why the feature cannot be
#: offered on this file ("" when it can): the feature is then left out and
#: the window lists it, with that reason, under the features it offers.
#: ``group`` can be a coding-variant copy key (:func:`.ents.variant_key`):
#: resolving names through :func:`.ents.one` handles it.
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

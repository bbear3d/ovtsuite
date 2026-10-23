# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The feature-switch engine: read the current state, plan a change, apply it.

Platform-agnostic. A bundle (:mod:`.model`) names its parameters; the engine
resolves each name to the entity -- or the per-dataset (EDC15) / per-coding
(EDC16) *copies* -- in the loaded file, reads the raw stored value, works out
which state the file is in, and, for the copies the user ticks, writes the
target state as ONE undo step (``session.transaction`` -> one ``Command``).
Raw stored values are compared and written, so the A2L conversion never
distorts a 0/1/2 switch. Checksums are corrected by the app on save.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import ents as _E
from . import model as M


@dataclass
class Change:
    param: str
    group: str                 # dataset / coding-copy label ("" = the only copy)
    addr: int
    bits: int
    old: int
    new: int
    prov: str

    @property
    def changed(self) -> bool:
        return self.old != self.new


# -- resolving parameters to the file's entities --------------------------

def _entities(session, name: str):
    """Every entity of ``name`` in the file, one per copy/dataset."""
    return _E.named(session, name)


def _hook_module(session, name: Optional[str]):
    """The table hook a bundle's ``table_hook`` names, from the platform's
    module (:func:`model.hook`), or None.

    A feature can be a pure table edit (no single-value switch parameters); its
    presence, copies and state then come from the hook, which exposes
    ``present``/``groups``/``state`` with the same meaning as here."""
    return M.hook(session, name)


def is_present(session, bundle: M.Bundle) -> bool:
    """Whether the file carries this feature at all: any of its parameters, or
    -- for a table-only feature -- the hook's table."""
    if present_params(session, bundle):
        return True
    h = _hook_module(session, bundle.table_hook)
    if h is not None and hasattr(h, "present"):
        try:
            return bool(h.present(session))
        except Exception:
            return False
    return False


def present_params(session, bundle: M.Bundle) -> List[M.Param]:
    """The bundle's parameters that this file actually carries (an optional
    one that the definition does not place is dropped)."""
    out = []
    for p in bundle.params:
        if _entities(session, p.name):
            out.append(p)
    return out


def groups(session, bundle: M.Bundle) -> List[str]:
    """The copy/dataset labels the bundle's parameters live in, in file order."""
    seen: List[str] = []
    for p in bundle.params:
        for e in _entities(session, p.name):
            g = getattr(e, "group", "") or ""
            if g not in seen:
                seen.append(g)
    if not seen:
        h = _hook_module(session, bundle.table_hook)
        if h is not None and hasattr(h, "groups"):
            try:
                seen = list(h.groups(session))
            except Exception:
                seen = []
    return seen


def _raw(session, e, bits: int) -> Optional[int]:
    try:
        return int(session.binfile.read(e.addr, bits, False))
    except Exception:
        return None


def _type_companion(session, val_name: str, group: str):
    """The ``X_swtYType_C`` entity paired with a switch's ``X_swtYVal_C`` in the
    same copy, or None. The Val/Type split is the format's own switch-pair
    structure (not a guess): the switch service reads {Val, Type}, and Type 1
    makes it read the EEPROM table instead of the flash Val, so a flash write
    is only authoritative with Type forced to flash (0/2 behave the same)."""
    if "_swt" not in val_name or not val_name.endswith("Val_C"):
        return None
    tname = val_name[:-len("Val_C")] + "Type_C"
    return _E.one(session, tname, group)


def read_values(session, bundle: M.Bundle, group: str) -> Dict[str, Optional[int]]:
    """Current raw value of each present parameter in one copy."""
    out: Dict[str, Optional[int]] = {}
    for p in present_params(session, bundle):
        e = next((x for x in _entities(session, p.name)
                  if (getattr(x, "group", "") or "") == group), None)
        out[p.name] = _raw(session, e, p.bits) if e is not None else None
    return out


def current_state(session, bundle: M.Bundle, group: str) -> Optional[str]:
    """Which state the copy matches on the parameters that discriminate it,
    or None when it matches none (a custom / tuned mix)."""
    # a table-only feature (no discriminating parameters) gets its state from
    # the hook module -- the parameters never move, the table does
    discriminating = [p for p in present_params(session, bundle)
                      if not p.readonly
                      and any(p.target(s) is not None for s in bundle.states)]
    if not discriminating:
        h = _hook_module(session, bundle.table_hook)
        if h is not None and hasattr(h, "state"):
            try:
                return h.state(session, group)
            except Exception:
                return None
    vals = read_values(session, bundle, group)
    for state in bundle.states:
        ok = True
        any_test = False
        for p in present_params(session, bundle):
            tgt = p.target(state)
            if tgt is None or p.readonly:
                continue
            any_test = True
            if vals.get(p.name) != int(tgt):
                ok = False
                break
        if any_test and ok:
            return state
    return None


# -- planning a change -----------------------------------------------------

def plan(session, bundle: M.Bundle, target_state: str,
         sel_groups: List[str]) -> Tuple[List[Change], List[str]]:
    """Byte writes to take ``sel_groups`` to ``target_state``, plus warnings.

    A parameter whose target value is None in this state is left untouched; a
    USER / DERIVED parameter with no concrete value becomes a warning (the
    caller must collect the value or run the table hook), never a blind write.
    """
    changes: List[Change] = []
    warnings: List[str] = []
    if bundle.readonly:
        return changes, ["This feature is informational and is not written."]
    params = present_params(session, bundle)
    for g in sel_groups:
        for p in params:
            if p.readonly:
                continue
            tgt = p.target(target_state)
            if tgt is None:
                continue
            if p.prov in (M.USER, M.DERIVED):
                warnings.append(
                    "%s needs a %s value for '%s' -- supply it before applying"
                    % (p.name, p.prov, target_state))
                continue
            e = next((x for x in _entities(session, p.name)
                      if (getattr(x, "group", "") or "") == g), None)
            if e is None:
                continue
            old = _raw(session, e, p.bits)
            if old is None:
                warnings.append("could not read %s (%s)" % (p.name, g or "only copy"))
                continue
            changes.append(Change(p.name, g, e.addr, p.bits, old, int(tgt), p.prov))
            # force the switch to its flash value: Type 1 reads the EEPROM
            # table, so a flash Val is only authoritative with Type = 2 (0
            # reads the flash too: left as it is)
            tc = _type_companion(session, p.name, g)
            if tc is not None:
                told = _raw(session, tc, 8)
                if told == 1:
                    changes.append(Change(tc.name, g, tc.addr, 8, told, 2,
                                          M.CODE))
    if bundle.table_hook:
        warnings.append(
            "This feature also resizes/populates a table (%s); run that step "
            "in the dialog." % bundle.table_hook)
    return changes, warnings


def apply(session, changes: List[Change], label: str):
    """Write the changed bytes as one undo step. Returns the number written."""
    live = [c for c in changes if c.changed]
    if not live:
        return 0
    from ..session import Command
    writes = [(c.addr, c.bits, c.old, c.new) for c in live]
    cmd = Command(label, writes)
    session._record(cmd, "%s (%d value%s)"
                    % (label, len(writes), "" if len(writes) == 1 else "s"))
    return len(writes)


__all__ = ["Change", "present_params", "is_present", "groups", "read_values",
           "current_state", "plan", "apply"]

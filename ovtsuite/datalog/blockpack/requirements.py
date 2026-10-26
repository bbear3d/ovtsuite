# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What a preset logs, as planner requirements, and what it reads today.

* A shipped recipe (:class:`ovtsuite.logtune.roles.Recipe`): one requirement
  per role, needs then wants, each satisfied by any of the role's signals
  (``logtune.roles.ROLES``) -- the same identities the logger plans presets on.
* A preset of your own (:class:`ovtsuite.datalog.userpresets.UserPreset`):
  every cell is a need for the exact signal it was saved with (one
  requirement per signal however many cells carried it). A cell saved with no
  signal cannot be found anywhere else, so it **pins** its block: the plan
  keeps that block as it is. (A preset's End of injection flag needs no
  requirement of its own: it works from the SOI and duration cells the
  preset already has.)

The baseline -- the groups read today -- comes from the logger's own
resolution on the stock view, so "fewer than today" means exactly what the
logger would otherwise read.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Mapping, Optional, Sequence

from .model import Requirement

#: Preset key of a shipped recipe is its recipe key; yours is the preset
#: box's item data (``userpresets.DATA_PREFIX`` + name).


def from_recipe(recipe, roles: Optional[Mapping[str, Sequence[str]]] = None,
                labels: Optional[Mapping[str, str]] = None) -> List[Requirement]:
    if roles is None or labels is None:
        from ...logtune import roles as _roles
        roles = _roles.ROLES if roles is None else roles
        labels = _roles.ROLE_LABELS if labels is None else labels
    out: List[Requirement] = []
    seen = set()
    for need, keys in ((True, recipe.needs), (False, recipe.wants)):
        for role in keys:
            if role in seen:
                continue
            seen.add(role)
            out.append(Requirement(role, tuple(roles.get(role, ())), need,
                                   labels.get(role, role)))
    return out


def from_user_preset(preset) -> List[Requirement]:
    out: List[Requirement] = []
    seen = set()
    for c in preset.cells:
        if c.signal:
            if c.signal in seen:
                continue
            seen.add(c.signal)
            out.append(Requirement(c.signal, (c.signal,), True, c.title or c.signal))
        else:
            key = "pin:%d/%d" % (c.group, c.field + 1)
            if key in seen:
                continue
            seen.add(key)
            out.append(Requirement(key, (), True,
                                   c.title or "Group %d / Cell %d" % (c.group, c.field + 1),
                                   pin=(int(c.group), int(c.field))))
    return out


def content_key(reqs: Sequence[Requirement]) -> str:
    """A short digest of what the requirements ask for. The manifest keeps it
    per preset: editing a preset changes it, which makes that preset's
    optimised layout stale (the logger then reads stock cells and the dialog
    offers to rebuild it)."""
    blob = json.dumps([[r.key, list(r.signals), bool(r.need),
                        list(r.pin) if r.pin else None] for r in reqs],
                      separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def recipe_baseline(namer, recipe, avoid: Sequence[int] = ()) -> List[int]:
    """The groups the logger reads for ``recipe`` on ``namer`` today."""
    from ...logtune.roles import recipe_groups
    return list(recipe_groups(namer, recipe, recipe.max_groups, avoid=avoid)[0])


def user_baseline(namer, preset, avoid: Sequence[int] = ()) -> List[int]:
    """The groups the logger reads for your ``preset`` on ``namer`` today."""
    from ..userpresets import resolve
    return sorted({int(g) for g, _f in resolve(preset, namer, avoid=avoid).cells})


def recipe_key(recipe) -> str:
    return recipe.key


def user_key(preset) -> str:
    return preset.data


__all__ = ["from_recipe", "from_user_preset", "content_key", "recipe_baseline",
           "user_baseline", "recipe_key", "user_key"]

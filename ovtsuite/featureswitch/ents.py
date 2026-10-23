# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Entities by name, for the feature switches.

A file carries ten to twenty thousand entities and the switches ask for a
few dozen names, over and over (every Tools-menu refresh asks which features
are present). One index per entity list -- rebuilt when a definition
replaces the list -- turns each of those scans into a dictionary lookup.
"""
from __future__ import annotations

from typing import Dict, List


def _index(session) -> Dict[str, List]:
    ents = getattr(session, "entities", None) or []
    cached = getattr(session, "_fsw_index", None)
    if cached is not None and cached[0] is ents:
        return cached[1]
    idx: Dict[str, List] = {}
    for e in ents:
        idx.setdefault(getattr(e, "name", ""), []).append(e)
    try:
        session._fsw_index = (ents, idx)
    except Exception:
        pass
    return idx


def named(session, name: str) -> List:
    """Every entity called ``name`` (one per dataset / coding copy)."""
    return list(_index(session).get(name, ()))


def one(session, name: str, group: str = ""):
    """The entity called ``name`` in copy ``group`` ("" = the only copy)."""
    return next((e for e in _index(session).get(name, ())
                 if (getattr(e, "group", "") or "") == group), None)


def group_of(e) -> str:
    return getattr(e, "group", "") or ""


__all__ = ["named", "one", "group_of"]

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
    """Every entity called ``name`` (one per dataset; the main copies only --
    a coding-variant copy is reached through :func:`one`)."""
    return list(_index(session).get(name, ()))


# -- coding-variant copies (EDC16) -------------------------------------------
# An EDC16 file can carry a second copy of some tables and values, read by
# other codings (``session.variant_entities()``: "<name> (variant @0x...)",
# with a ``coding`` attribute). The switches treat that copy as one more
# *copy* of the parameter, under the copy key ``variant_key(group)``; a
# parameter with no variant copy is shared: every coding reads the main one.
VARIANT = "coding variant"


def variant_key(group: str = "") -> str:
    """The copy key of ``group``'s coding-variant copy."""
    return "%s / %s" % (group, VARIANT) if group else VARIANT


def is_variant(group: str) -> bool:
    return bool(group) and (group == VARIANT or group.endswith(" / " + VARIANT))


def base_group(group: str) -> str:
    """The dataset part of a copy key ("" for the main / only copy)."""
    if not is_variant(group):
        return group or ""
    return "" if group == VARIANT else group[:-len(" / " + VARIANT)]


def _variant_index(session) -> Dict[str, List]:
    fn = getattr(session, "variant_entities", None)
    if fn is None:
        return {}
    try:
        vs = fn() or []
    except Exception:
        return {}
    cached = getattr(session, "_fsw_vindex", None)
    if cached is not None and cached[0] is vs and cached[1] == len(vs):
        return cached[2]
    idx: Dict[str, List] = {}
    for v in vs:
        idx.setdefault((getattr(v, "name", "") or "").split(" (variant @", 1)[0],
                       []).append(v)
    try:
        session._fsw_vindex = (vs, len(vs), idx)
    except Exception:
        pass
    return idx


def has_variants(session) -> bool:
    """Whether the file carries coding-variant copies at all."""
    return bool(_variant_index(session))


def variant_of(session, name: str, group: str = ""):
    """The coding-variant copy of ``name`` in dataset ``group``, or None."""
    return next((v for v in _variant_index(session).get(name, ())
                 if group_of(v) == base_group(group)), None)


def one(session, name: str, group: str = ""):
    """The entity called ``name`` in copy ``group`` ("" = the only copy). A
    coding-variant copy key gives the variant copy, or -- for a parameter
    that has none -- the main one every coding reads."""
    if is_variant(group):
        v = variant_of(session, name, group)
        if v is not None:
            return v
        group = base_group(group)
    return next((e for e in _index(session).get(name, ())
                 if (getattr(e, "group", "") or "") == group), None)


def shared(session, name: str, group: str) -> bool:
    """True when copy ``group`` of ``name`` is the main copy read by every
    coding (a variant key for a parameter without a variant copy)."""
    return is_variant(group) and variant_of(session, name, group) is None


def group_of(e) -> str:
    return getattr(e, "group", "") or ""


__all__ = ["named", "one", "group_of", "VARIANT", "variant_key", "is_variant",
           "base_group", "has_variants", "variant_of", "shared"]

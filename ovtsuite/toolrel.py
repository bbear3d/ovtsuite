# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which per-file tools apply to a session -- cheap answers for the Tools
menu, which asks on every tab switch.

An answer is kept on the session against its entity list, so a definition
applied later (an unrecognised file given one) is asked about afresh.
"""


def _cached(session, key, fn) -> bool:
    ents = getattr(session, "entities", None)
    store = getattr(session, "_tool_rel", None)
    if store is None or store.get("_ents") is not ents:
        store = {"_ents": ents}
        try:
            session._tool_rel = store
        except Exception:
            pass
    if key not in store:
        try:
            store[key] = bool(fn())
        except Exception:
            store[key] = False
    return store[key]


def has_gear_ratios(session) -> bool:
    """Tools > Gear ratios: the engine ECU's drivetrain ratios."""
    if session is None:
        return False

    def probe():
        from .gears import GearRatios
        return GearRatios(session).available
    return _cached(session, "gears", probe)


def has_vss(session) -> bool:
    """Tools > VSS pulse settings: an engine ECU's vehicle-speed scaling,
    or a VDO cluster's K-value."""
    if session is None:
        return False

    def probe():
        plat = getattr(session, "platform", "")
        if plat == "vdo":
            from .vdo import speed
            return speed.k_value(session) is not None
        if getattr(session, "is_edc15", False):
            from .edc15.vss15 import speed_settings
            return speed_settings(session).available
        if plat in ("zf6hp", "zf8hp", "zf5hp", "aisin", "hvac"):
            return False
        return any(getattr(e, "name", "").startswith("VSSCD_ImpPerDist")
                   for e in (session.entities or ()))
    return _cached(session, "vss", probe)

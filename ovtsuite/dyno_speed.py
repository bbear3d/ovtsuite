# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Road speed for the dyno chart: km/h per 1000 rpm in each gear, and the
condition presets.

Road speed comes from a **gearbox** -- its gear ratios and final drive(s),
from the catalogue (``data/gearboxes.json``: common VAG gearboxes, each with
its source) or one of the user's own -- and the **tyre**: km/h per 1000 rpm
= 60 x rolling circumference (m) / (gear ratio x final drive).

EDC16 also keeps a gear model of its own: each gear's overall ratio
(``Gearbx_rTrans<n>_C``) and its gear-recognition bands, from which a
rolling circumference follows (:meth:`ovtsuite.gears.GearRatios.
effective_circ`). It is the ECU's model for recognising the gear and is
often not the car's gearbox (03G906016LQ's main block is a CVT's), so it is
offered as a choice, said to be the ECU's, never taken as the car's. EDC15
keeps no ratios at all -- only the v/n thresholds between gears -- so there
the gearbox is always chosen.
"""

from __future__ import annotations

import json
import math
import os
import re
from typing import Dict, List, Optional, Tuple

#: speed units: (key, label, factor from km/h)
SPEED_UNITS = (("kmh", "km/h", 1.0), ("mph", "mph", 1 / 1.609344))
#: the gearbox choice that means the ECU's own gear model (EDC16)
ECU = "ecu"
#: a tyre's rolling circumference against its unloaded one (the tyre
#: flattens under load; ~3 % is the usual allowance)
ROLLING = 0.97
USER_GEARBOXES_KEY = "dyno/gearboxes"
DEFAULT_TYRE = "205/55 R16"


def speed_label(unit: str) -> str:
    return next((lbl for k, lbl, _f in SPEED_UNITS if k == unit), "km/h")


def speed_in(kmh: float, unit: str) -> float:
    return kmh * next((f for k, _l, f in SPEED_UNITS if k == unit), 1.0)


# ------------------------------------------------------------------ tyres
_TYRE = re.compile(r"^\s*(\d{3})\s*/\s*(\d{2})\s*Z?R?\s*(\d{2}(?:\.\d)?)\s*$", re.I)


def tyre_circ(text: str) -> Optional[float]:
    """The rolling circumference (m) of a tyre size such as ``225/45 R17``,
    or of a circumference given in metres (``1.93``); None when neither."""
    t = (text or "").strip().replace(",", ".")
    m = _TYRE.match(t)
    if m:
        width, aspect, rim = float(m.group(1)), float(m.group(2)), float(m.group(3))
        d = rim * 25.4 + 2 * width * aspect / 100.0          # mm
        return math.pi * d / 1000.0 * ROLLING
    try:
        v = float(t.rstrip("m").strip())
    except ValueError:
        return None
    return v if 1.0 <= v <= 3.5 else None


# --------------------------------------------------------------- gearboxes
def _data_path() -> str:
    try:
        from . import datafiles
        got = datafiles.path("gearboxes.json")
    except Exception:
        got = None
    return got or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                               "gearboxes.json")


_CATALOGUE: Optional[List[dict]] = None


def catalogue() -> List[dict]:
    """The built-in gearboxes: ``{"id", "name", "gears": [..], "finals":
    [..], "final_of": [index of the final drive each gear uses], "source",
    "note"}``."""
    global _CATALOGUE
    if _CATALOGUE is None:
        try:
            with open(_data_path(), encoding="utf-8") as fh:
                _CATALOGUE = [g for g in json.load(fh).get("gearboxes", [])
                              if _valid(g)]
        except (OSError, ValueError):
            _CATALOGUE = []
    return _CATALOGUE


def _valid(g) -> bool:
    try:
        return bool(g["id"]) and len(g["gears"]) >= 1 and all(
            float(x) > 0 for x in list(g["gears"]) + list(g["finals"]))
    except (KeyError, TypeError, ValueError):
        return False


def user_gearboxes() -> List[dict]:
    from .appsettings import settings
    try:
        raw = settings().value(USER_GEARBOXES_KEY, "") or ""
        items = json.loads(raw) if raw else []
        return [dict(g, user=True) for g in items if _valid(g)]
    except Exception:
        return []


def save_user_gearboxes(items) -> None:
    from .appsettings import settings
    s = settings()
    s.setValue(USER_GEARBOXES_KEY, json.dumps(
        [{k: v for k, v in g.items() if k != "user"} for g in items]))
    s.sync()


def gearbox(gid: str) -> Optional[dict]:
    return next((g for g in user_gearboxes() + catalogue() if g["id"] == gid), None)


def overall(gb: dict) -> Dict[int, float]:
    """``{gear: gear ratio x its final drive}``."""
    finals = [float(x) for x in gb["finals"]]
    of = list(gb.get("final_of") or [])
    out = {}
    for i, r in enumerate(gb["gears"], 1):
        fi = of[i - 1] if i - 1 < len(of) else 0
        out[i] = float(r) * finals[min(max(0, int(fi)), len(finals) - 1)]
    return out


def gearbox_speeds(gb: dict, circ: float) -> Dict[int, float]:
    """``{gear: km/h per 1000 rpm}`` of gearbox ``gb`` on a tyre of
    rolling circumference ``circ`` (m)."""
    return {g: 60.0 * circ / r for g, r in overall(gb).items() if r > 0}


def parse_ratios(text: str) -> List[float]:
    """``"3.78 2.12, 1.36; 0.97"`` -> [3.78, 2.12, 1.36, 0.97]."""
    out = []
    for tok in re.split(r"[\s,;/]+", (text or "").strip()):
        if tok:
            v = float(tok)
            if v <= 0:
                raise ValueError("a ratio must be above zero: %s" % tok)
            out.append(v)
    return out


def ecu_gears(session) -> Tuple[Dict[int, float], str]:
    """``({gear: km/h per 1000 rpm}, where from)`` from the ECU's own gear
    model (EDC16); ``({}, why not)`` when the file keeps none."""
    if session is None:
        return {}, "no file"
    if getattr(session, "is_edc15", False):
        return {}, "EDC15 keeps no gear ratios"
    try:
        from .gears import GearRatios
        g = GearRatios(session)
        if not g.available:
            return {}, "the file has no gear ratios"
        copy = g.copies()[0]
        ratios = g._ratios(copy)
        circ = g.effective_circ(copy)
        if not ratios or not circ:
            return {}, "the gear ratios or the gear-recognition bands are missing"
        return ({n: 60.0 * circ / r for n, r in sorted(ratios.items())},
                "the ECU's gear model (%s: Gearbx_rTrans, %.3f m from the "
                "gear-recognition bands)" % (copy.label, circ))
    except Exception as ex:
        return {}, "the gear ratios could not be read (%s)" % ex


#: (kept for callers of the first version)
file_gears = ecu_gears


# --------------------------------------------------------------- presets
#: built-in conditions: (name, the fields they set)
PRESETS: List[Tuple[str, Dict[str, float]]] = [
    ("Standard: sea level, warm", {"baro": 1013.0, "charge_temp": 50.0,
                                   "coolant": 90.0, "loss": 0.0}),
    ("Cold day (charge 30 °C)", {"baro": 1013.0, "charge_temp": 30.0}),
    ("Hot day (charge 65 °C)", {"baro": 1013.0, "charge_temp": 65.0}),
    ("Altitude 1000 m (899 hPa)", {"baro": 899.0}),
    ("Altitude 2000 m (795 hPa)", {"baro": 795.0}),
    ("At the wheels (15 % loss)", {"loss": 15.0}),
    ("At the crank (no loss)", {"loss": 0.0}),
]
#: the fields a saved preset keeps
PRESET_FIELDS = ("baro", "charge_temp", "coolant", "ve", "efficiency", "loss",
                 "boost_cap")
USER_KEY = "dyno/presets"


def user_presets() -> List[Tuple[str, Dict[str, float]]]:
    import json
    from .appsettings import settings
    try:
        raw = settings().value(USER_KEY, "") or ""
        items = json.loads(raw) if raw else []
        return [(str(n), {k: v for k, v in dict(f).items() if k in PRESET_FIELDS})
                for n, f in items]
    except Exception:
        return []


def save_user_presets(items) -> None:
    import json
    from .appsettings import settings
    s = settings()
    s.setValue(USER_KEY, json.dumps([[n, f] for n, f in items]))
    s.sync()


def preset_of(cond) -> Dict[str, float]:
    return {k: getattr(cond, k) for k in PRESET_FIELDS}


__all__ = ["SPEED_UNITS", "speed_label", "speed_in", "ecu_gears", "file_gears",
           "tyre_circ", "catalogue", "user_gearboxes", "save_user_gearboxes",
           "gearbox", "overall", "gearbox_speeds", "parse_ratios", "ECU", "PRESETS",
           "PRESET_FIELDS", "user_presets", "save_user_presets", "preset_of"]

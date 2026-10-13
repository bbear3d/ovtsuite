# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Speed-map display modes for transmission tables (base app).

A TCU's shift / TCC / tip-limit maps store output-shaft speed; the grid can
show it as engine rpm (converter locked) or road speed instead. The modes, the
vehicle (final drive + tyre) and the generic conversion live here, in the
base app, so they work for any module whose entities say their Z is a speed
(``entity.z_speed``, with per-column gear ratios in ``entity.col_ratios`` --
the ZF 8HP labels do exactly that).

The ZF 6HP module knows more -- families recognised by symbol, TCC sub-column
geometry, coding-bank pairs -- in :mod:`ovtsuite.zf6hp.view6hp`. When that
module is installed every call here goes to it; when it is not, the generic
behaviour below applies, so an 8HP-only install keeps its speed views and
nothing in the GUI has to know which modules exist.
"""

import math
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

#: Z display modes, in menu order. ``output`` is the stored value itself --
#: these maps hold output-shaft speed, so "as stored" and "output shaft speed"
#: are the same number and offering both would be two names for one thing.
#: ``raw`` remains as the internal no-conversion mode for every other kind of
#: map, and is not offered in the menu.
Z_MODES = ("output", "engine", "kmh", "mph")

Z_MODE_LABELS = {
    "raw": "Stored value",
    "output": "Output shaft speed (RPM) — as stored",
    "engine": "Engine speed (RPM), converter locked",
    "kmh": "Road speed (km/h)",
    "mph": "Road speed (mph)",
}

#: What the engine-speed figure assumes, said once and shown wherever it is
#: offered. Turbine speed equals output-shaft speed times the gear ratio only
#: while the torque converter is locked; with it open the converter slips and
#: the engine turns faster than this by the slip, which no calibration map
#: records. So the number is exact for a locked shift and a lower bound for an
#: unlocked one -- worth saying, because the difference is hundreds of RPM at
#: low speed.
ENGINE_MODE_CAVEAT = (
    "Engine speed is the output-shaft speed times the gear ratio of the gear "
    "the column leaves, which is exact only while the torque converter is "
    "locked. With the converter open the engine turns faster than this by the "
    "slip.")


#: The mode a speed map opens in until the user picks another.
DEFAULT_MODE = "output"


# --- road speed ------------------------------------------------------------

def vehicle_speed_kmh(raw_output_rpm: float, axle_ratio: float,
                      tire_circumference_m: float) -> float:
    """Output-shaft RPM to road speed in km/h.

    ``axle_ratio`` is the final drive (wheel turns = shaft turns / ratio);
    ``tire_circumference_m`` is the driven-wheel rolling circumference.
    """
    return (raw_output_rpm / axle_ratio) * tire_circumference_m * 0.06


def vehicle_speed_mph(kmh: float) -> float:
    return kmh / 1.609344


TIRE_SIZE_RE = re.compile(
    r"^\s*(\d{2,4})\s*/\s*(\d{2,3})\s*[A-Za-z]?\s*[Rr]\s*(\d{1,2})(?:\.\d+)?\s*$")


def parse_tire_size(s: str):
    """``225/50R17`` -> ``(width_mm, aspect_pct, rim_in)``."""
    m = TIRE_SIZE_RE.match(s)
    if not m:
        raise ValueError("cannot parse tyre size %r (expected e.g. 225/50R17)" % s)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def tire_circumference_mm(width_mm: int, aspect_pct: int, rim_in: int) -> float:
    """Nominal rolling circumference in millimetres."""
    return math.pi * (rim_in * 25.4 + 2 * width_mm * (aspect_pct / 100.0))


@dataclass(frozen=True)
class Vehicle:
    """What road speed needs beyond the transmission.

    The defaults are a starting point for the dialog, not a claim about the
    car. Where the calibration states its own final drive, :func:`vehicle_of`
    reads it instead of asking.
    """
    axle_ratio: float = 4.16
    tyre: str = "255/40R19"

    @property
    def circumference_m(self) -> float:
        return tire_circumference_mm(*parse_tire_size(self.tyre)) / 1000.0


# --- dispatch ----------------------------------------------------------------

_EXT = False


def _ext():
    """The ZF 6HP view module when installed, else None (looked up once)."""
    global _EXT
    if _EXT is False:
        try:
            from .zf6hp import view6hp as ext
        except ImportError:
            ext = None
        _EXT = ext
    return _EXT


def is_speed_map(entity) -> bool:
    e = _ext()
    if e is not None:
        return e.is_speed_map(entity)
    return bool(getattr(entity, "z_speed", False))


def z_modes(entity) -> Tuple[str, ...]:
    """The display modes offered for this entity -- empty when Z is not a
    speed, so the selector is hidden rather than showing meaningless options."""
    return Z_MODES if is_speed_map(entity) else ()


def z_factor(entity, col: int, mode: str = "raw",
             vehicle: Optional[Vehicle] = None) -> float:
    """The multiplier that turns a stored Z value into ``mode`` (see
    :func:`ovtsuite.zf6hp.view6hp.z_factor`)."""
    e = _ext()
    if e is not None:
        return e.z_factor(entity, col, mode, vehicle)
    if mode in ("raw", "output") or not is_speed_map(entity):
        return 1.0
    if mode == "engine":
        own = getattr(entity, "col_ratios", None)
        if own and 0 <= col < len(own):
            return float(own[col])
        return 1.0
    vehicle = vehicle or Vehicle()
    kmh = vehicle_speed_kmh(1.0, vehicle.axle_ratio, vehicle.circumference_m)
    return kmh if mode == "kmh" else vehicle_speed_mph(kmh)


#: How a stored "off" marker reads in a converted mode. It is never scaled:
#: 10000 output rpm meaning "this threshold is disabled" is not a speed, and
#: times a first-gear ratio it read as 47,000 engine rpm.
OFF_TEXT = "off"


def z_off(entity) -> Optional[float]:
    """The stored Z value that means "off" (threshold disabled, shift never
    made) on this speed map, or None. Stated by the module that knows the
    format: ``entity.z_off`` (the ZF 8HP shift programs and WK tables carry
    it), or the ZF 6HP view module's own rule."""
    e = _ext()
    if e is not None and hasattr(e, "z_off"):     # an older 6HP module has none
        return e.z_off(entity)
    off = getattr(entity, "z_off", None)
    return float(off) if off is not None and is_speed_map(entity) else None


def is_off(entity, value) -> bool:
    """True when the stored ``value`` is this map's "off" marker."""
    if value is None:
        return False
    off = z_off(entity)
    return off is not None and abs(float(value) - off) < 1e-6


def is_converted(mode: str) -> bool:
    """True for a mode that scales Z (engine / road speed)."""
    return mode not in ("raw", "output")


def convert_z(entity, value: float, col: int,
              mode: str = "raw", vehicle: Optional[Vehicle] = None) -> float:
    """``value`` in ``mode``; an "off" marker stays as stored."""
    if is_converted(mode) and is_off(entity, value):
        return float(value)
    return float(value) * z_factor(entity, col, mode, vehicle)


def invert_z(entity, shown: float, col: int,
             mode: str = "raw", vehicle: Optional[Vehicle] = None) -> float:
    f = z_factor(entity, col, mode, vehicle)
    return float(shown) / f if f else float(shown)


def z_math(entity) -> List[Tuple[int, str]]:
    """Per-column ``<MATH>`` overrides for an exported speed map (1-based)."""
    e = _ext()
    if e is not None:
        return e.z_math(entity)
    if not is_speed_map(entity):
        return []
    return [(c + 1, "X * %g" % z_factor(entity, c, "engine"))
            for c in range(int(getattr(entity, "cols", 0) or 0))]


def compare_columns(primary, other):
    """Destination -> source column map for a comparison overlay, or None
    (interpolate as usual)."""
    e = _ext()
    return e.compare_columns(primary, other) if e is not None else None


def column_headers(entity) -> Optional[List[str]]:
    e = _ext()
    return e.column_headers(entity) if e is not None else None


def row_headers(entity, y_values=None) -> Optional[List[str]]:
    e = _ext()
    return e.row_headers(entity, y_values) if e is not None else None


def paired_entity(entity, by_name):
    """The other coding bank's entity (ZF 6HP), or None."""
    e = _ext()
    return e.paired_entity(entity, by_name) if e is not None else None


def index_by_name(entities) -> dict:
    """symbol -> entity, first wins."""
    out = {}
    for e in entities:
        out.setdefault(e.name, e)
    return out


def mirror_cells(entity, other):
    e = _ext()
    return e.mirror_cells(entity, other) if e is not None else []


def axle_ratio_of(session) -> Optional[float]:
    """The final drive the calibration states (ZF 6HP), or None."""
    e = _ext()
    return e.axle_ratio_of(session) if e is not None else None


def vehicle_of(session, fallback: Optional[Vehicle] = None) -> Vehicle:
    """The vehicle to convert road speed with: the file's own final drive
    where it states one, otherwise whatever the user set."""
    base = fallback or Vehicle()
    stated = axle_ratio_of(session)
    return Vehicle(axle_ratio=stated, tyre=base.tyre) if stated else base

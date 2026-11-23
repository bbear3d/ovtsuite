# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measured value block 0 of a distributor-pump TDI (EDC15VM+, VP37).

KW1281 group 0 is ten raw bytes with no formula sent along (block ``F4``).
What each byte is comes from the ECU's own table ``xcwMessw01..10`` (the
message number of measured value 1..10), resolved through the message table
to a RAM variable and the scaling descriptor the firmware formats it with
(``b = value * a / 10000 + b0``, ``a`` signed). Read from 038906012CP / Q /
BB (program A120, DAMOS ``A12005BG.DAM``) and 038906012A, and checked
against the workshop manual's group 000 tables for engine codes AGR, AHF,
ALH, ASV and AGP, AQM -- every field and every limit agrees:

====  =========================  ==================  =====================
byte  value                      RAM (A120)          physical
====  =========================  ==================  =====================
 1    engine speed               dzmNmit             byte * 20.83 rpm
 2    start of injection actual  sbmPHIist           (byte - 38) * 0.08
                                                     deg, + = BTDC
 3    accelerator pedal          anmPWG              byte / 2.55 %
 4    quantity injected          mrmM_EAKT           byte * 0.2 mg/stroke
 5    intake manifold pressure   ldmP_Llin           byte * 10.2 mbar
 6    atmospheric pressure       anmADF              byte * 4.968 mbar
 7    coolant temperature        anmWTF              134.4 - 0.685 * byte C
 8    intake air temperature     anmLTF              134.4 - 0.685 * byte C
 9    fuel temperature           anmKTF              byte * 0.5525 - 30.05 C
10    air mass                   armM_List           byte * 3.333 mg/stroke
====  =========================  ==================  =====================

The temperatures 7 and 8 fall as the byte rises (the scaling word is
negative), as the manual's "80...35 = 80...110 C" shows.

The manual states its limits in display bytes, so :func:`check` compares
bytes, not converted values: a limit is exactly what the manual says.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class Field:
    name: str
    unit: str
    scale: float
    offset: float
    decimals: int = 0

    def value(self, byte: int) -> float:
        return byte * self.scale + self.offset


#: the ten bytes of group 0 on an EDC15VM+ (VP37) engine
VE_FIELDS: Tuple[Field, ...] = (
    Field("Engine speed", "rpm", 10000.0 / 480, 0.0),
    Field("Start of injection", "deg BTDC", 0.08, -38 * 0.08, 1),
    Field("Accelerator pedal", "%", 100.0 / 255, 0.0),
    Field("Quantity injected", "mg/stroke", 0.2, 0.0, 1),
    Field("Intake manifold pressure", "mbar", 10000.0 / 980, 0.0),
    Field("Atmospheric pressure", "mbar", 10000.0 / 2013, 0.0),
    Field("Coolant temperature", "C", -1000.0 / 1460, 595 * 1000.0 / 1460 - 273.14, 1),
    Field("Intake air temperature", "C", -1000.0 / 1460, 595 * 1000.0 / 1460 - 273.14, 1),
    Field("Fuel temperature", "C", 1000.0 / 1810, 440 * 1000.0 / 1810 - 273.14, 1),
    Field("Air mass", "mg/stroke", 1000.0 / 300, 0.0),
)

LAYOUTS = {"edc15vm": VE_FIELDS}

#: Workshop-manual limits, in display bytes (inclusive), per engine-code
#: group and condition. None: "ignore". Byte ranges as printed (a falling
#: range such as 80...35 is a temperature; it is stored low..high).
SPECS: Dict[str, Dict[str, Dict[int, Optional[Tuple[int, int]]]]] = {
    "ahf": {        # AGR, AHF, ALH, ASV
        "idle": {1: (41, 46), 2: (12, 75), 3: (0, 0), 4: (11, 45), 5: (83, 106),
                 6: (183, 224), 7: (35, 80), 8: (50, 182), 9: (91, 201), 10: (69, 111)},
        "full": {1: (138, 148), 2: (137, 215), 3: (255, 255), 4: (199, 214),
                 5: (179, 215), 6: (183, 224), 7: (35, 80), 8: (50, 182),
                 9: (91, 201), 10: (255, 255)},
    },
    "agp": {        # AGP, AQM
        "idle": {1: (41, 46), 2: (25, 125), 3: (0, 0), 4: (11, 45), 5: None,
                 6: (181, 222), 7: (35, 80), 8: (51, 182), 9: (91, 201), 10: None},
        "full": {1: (138, 148), 2: (113, 163), 3: (255, 255), 4: (125, 140),
                 5: None, 6: (181, 222), 7: (35, 80), 8: (50, 182), 9: (91, 201),
                 10: None},
    },
}

ENGINE_GROUPS = {"ahf": "AGR, AHF, ALH, ASV", "agp": "AGP, AQM"}


def fields(layout: str) -> Tuple[Field, ...]:
    try:
        return LAYOUTS[layout]
    except KeyError:
        raise KeyError("no group 0 layout %r (known: %s)"
                       % (layout, ", ".join(sorted(LAYOUTS))))


def fmt(f: Field, v: float) -> str:
    return "%.*f %s" % (f.decimals, v, f.unit)


def decode(raw: Sequence[int], layout: str = "edc15vm") -> List[Tuple[Field, int, float]]:
    """``[(field, byte, value)]`` for the bytes of a group 0 read."""
    fs = fields(layout)
    return [(f, int(b), f.value(int(b))) for f, b in zip(fs, list(raw)[:len(fs)])]


def spec_text(f: Field, rng: Optional[Tuple[int, int]]) -> str:
    """A limit as the manual prints it, with what it means in units."""
    if rng is None:
        return "ignore"
    lo, hi = rng
    a, b = sorted((f.value(lo), f.value(hi)))
    if lo == hi:
        return "%d (%s)" % (lo, fmt(f, a))
    return "%d...%d (%s ... %s)" % (lo, hi, fmt(f, a), fmt(f, b))


def check(raw: Sequence[int], engines: str, condition: str,
          layout: str = "edc15vm") -> List[Tuple[int, Field, int, Optional[bool], str]]:
    """``[(field number, field, byte, ok, spec text)]``: each byte against
    the manual's limit (``ok`` None where the manual says ignore)."""
    spec = SPECS[engines][condition]
    out = []
    for i, (f, b, _v) in enumerate(decode(raw, layout), start=1):
        rng = spec.get(i)
        ok = None if rng is None else rng[0] <= b <= rng[1]
        out.append((i, f, b, ok, spec_text(f, rng)))
    return out


def value_band(engines: str, condition: str, field_no: int,
               layout: str = "edc15vm") -> Optional[Tuple[float, float]]:
    """The manual's limit for one field in its units (low, high), or None."""
    rng = SPECS[engines][condition].get(field_no)
    if rng is None:
        return None
    f = fields(layout)[field_no - 1]
    return tuple(sorted((f.value(rng[0]), f.value(rng[1]))))     # type: ignore[return-value]


__all__ = ["Field", "VE_FIELDS", "LAYOUTS", "SPECS", "ENGINE_GROUPS", "decode",
           "check", "spec_text", "value_band", "fields", "fmt"]

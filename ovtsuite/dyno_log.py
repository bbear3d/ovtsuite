# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A data log as a dyno run: what the engine was given at full load.

The log's injected quantity (``iq``) at each engine speed, where the pedal
is (nearly) floored, is read through the same conversion as the calculated
curves (the reference's torque-to-fuel map, or the fuel-energy model), so
the measured run sits on the same scale as the predicted one. Samples are
gathered into engine-speed bins; a bin's quantity is the upper quartile of
its samples (the pull, not the gear change or the lift), and a bin with
fewer than :data:`MIN_SAMPLES` is left out.

Nothing here is measured torque: it is the quantity the ECU delivered,
converted as the chart converts its own.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from . import dyno as _dy

#: pedal at or above this (%) counts as full load
PEDAL_MIN = 90.0
#: engine-speed bin width (rpm)
BIN = 100.0
MIN_SAMPLES = 2
#: the limiter key of a measured point
KEY = "log"


def full_load(runs, pedal_min: float = PEDAL_MIN, width: float = BIN
              ) -> Tuple[List[Tuple[float, float, Optional[float]]], str]:
    """``([(rpm, quantity mg, boost hPa or None)], note)``: the full-load
    samples of ``runs`` (:class:`ovtsuite.logtune.samples.Run`), binned."""
    bins: Dict[float, List[Tuple[float, Optional[float]]]] = {}
    with_pedal = any(r.has("pedal") for r in runs)
    usable = 0
    for r in runs:
        if not (r.has("rpm") and r.has("iq")):
            continue
        usable += 1
        for i in range(len(r)):
            n, q = r.get("rpm", i), r.get("iq", i)
            if n is None or q is None or n < 500 or q <= 0:
                continue
            if with_pedal:
                ped = r.get("pedal", i)
                if ped is None or ped < pedal_min:
                    continue
            b = round(n / width) * width
            bins.setdefault(b, []).append((q, r.get("boost", i)))
    if not usable:
        return [], "the log has no engine speed and injected quantity"
    out = []
    for b in sorted(bins):
        s = sorted(bins[b], key=lambda x: x[0])
        if len(s) < MIN_SAMPLES:
            continue
        q, boost = s[min(len(s) - 1, int(len(s) * 0.75))]
        out.append((b, q, boost))
    if not out:
        return [], ("no full-load samples (pedal at %.0f %% or more)" % pedal_min
                    if with_pedal else "too few samples")
    note = ("full load: pedal at %.0f %% or more" % pedal_min) if with_pedal else \
        "the log has no pedal position: every sample is taken"
    return out, note


def curve(points: Sequence[Tuple[float, float, Optional[float]]], model,
          reference, cond: Optional[_dy.Conditions], label: str, note: str = ""
          ) -> _dy.Curve:
    """The binned samples as a :class:`~ovtsuite.dyno.Curve`, torque from
    ``reference``'s conversion (``model``'s own, or fuel energy, as
    :func:`ovtsuite.dyno.calculate` does)."""
    cond = cond or _dy.Conditions()
    conv = reference if (reference is not None and reference.has_conversion) \
        else (model if model is not None and model.has_conversion else None)
    cyl = model.engine(cond)[1] if model is not None else (cond.cylinders or 0)
    keep = 1.0 - max(0.0, min(cond.loss, 90.0)) / 100.0
    pts = []
    for rpm, q, boost in points:
        t = None
        ext = False
        if conv is not None:
            r = conv.torque_of(rpm, q, cond)
            if r is not None:
                t, ext = r
        if t is None:
            t = _dy.fuel_torque(q, cyl, cond.efficiency) if cyl else 0.0
        t = max(0.0, t) * keep
        pts.append(_dy.Point(rpm, q, t, _dy.power_kw(t, rpm), KEY, {KEY: q},
                             boost=boost, extrapolated=ext))
    lim = {KEY: _dy.Limiter(KEY, "Logged quantity",
                            tip="what the ECU delivered, from the log")}
    notes = [n for n in (note,) if n]
    return _dy.Curve(label, pts, lim, "the logged quantity through "
                     + (("the %s" % conv.conversion_name) if conv is not None
                        else "the fuel-energy model"), notes)


__all__ = ["full_load", "curve", "PEDAL_MIN", "BIN", "KEY"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""A dyno run's fitted hardware (:mod:`ovtsuite.gui_dyno`) when no project
records it: what the calibration itself looks set up for.

* a unit-injector (PD) file: its duration maps against the injector presets
  (:func:`ovtsuite.injectors.identify_datasets`);
* an EDC15 VE (VP37) file: its pump voltage map against the factory maps
  (the EDC15 module's ``vepump15.identify_all``).

Hardware worked out this way is **inferred**: its records carry
``source == FROM_MAPS`` and every text about them says "(from the maps)".
``extra["confident"]`` is True only for an exact match -- a PD file whose
duration maps are a preset's own (:attr:`ovtsuite.injectors.Match.exact`),
a VE map that is a factory map unchanged (``kind == "factory"``). The dyno
uses a confident inference like a record to stop carrying the quantity
across the reference's duration maps; a close one only in its notes.

Results are kept per file content (and, for PD, per duration-map
characteristic, the slow part), so the chart's polling does not identify a
file again until its bytes change.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Dict, List, Optional

from .project import hardware as _hw

#: ``Fitted.source`` of a record worked out from the maps (never stored)
FROM_MAPS = "maps"

_KEEP = 64
_files: "OrderedDict[tuple, Dict[str, Optional[object]]]" = OrderedDict()
_chars: "OrderedDict[str, tuple]" = OrderedDict()


class HardwareOf(list):
    """A run's hardware (a list of :class:`ovtsuite.project.hardware.Fitted`),
    with the project and set its records come from (for their descriptions;
    None for hardware from the maps)."""

    def __init__(self, recs=(), project=None, sid: str = ""):
        super().__init__(recs)
        self.project, self.sid = project, sid


def is_inferred(rec) -> bool:
    return getattr(rec, "source", "") == FROM_MAPS


def is_confident(rec) -> bool:
    """A record, or an inference from an exact match."""
    return not is_inferred(rec) or bool((rec.extra or {}).get("confident"))


def inferred(got) -> bool:
    return bool(got) and any(is_inferred(x) for x in got)


# ------------------------------------------------------------------ words
def short(rec) -> str:
    """The record's one line; an inferred one says so."""
    if not is_inferred(rec):
        return rec.summary
    return "looks set up for %s (from the maps%s)" % (
        rec.summary, "" if is_confident(rec) else ", not an exact match")


def describe(rec, project=None, sid: str = "") -> str:
    if is_inferred(rec):
        why = (rec.extra or {}).get("why", "")
        return short(rec) + (" -- " + why if why else "")
    return _hw.describe(rec, project, sid)


# ------------------------------------------------------------- comparing
def _alike(x, y) -> bool:
    """Do ``x`` and ``y`` (same kind, one of them inferred) say the same
    hardware, as far as both say it?"""
    px, py = x.parts or {}, y.parts or {}
    if x.kind == _hw.PD_INJECTORS:
        a, b = px.get("preset"), py.get("preset")
        return a == b if a and b else True          # a record by hand: not told
    if x.kind == _hw.VE_PUMP:
        a, b = px.get("plunger_mm"), py.get("plunger_mm")
        if a and b and abs(float(a) - float(b)) > 0.05:
            return False
        a, b = px.get("nozzle"), py.get("nozzle")
        return not (a and b and a != b)
    return px == py


def differ(a, b) -> List[str]:
    """The kinds whose hardware is not the same in ``a`` and ``b``. Two
    records lists: :func:`ovtsuite.project.hardware.differ` (a kind one side
    has no record of is "as it came", so different). With hardware from the
    maps on either side, only the kinds both say anything about, compared by
    what both say (the preset; the pump head and the nozzle)."""
    if not inferred(a) and not inferred(b):
        return _hw.differ(list(a), list(b))
    ka = {r.kind: r for r in a}
    kb = {r.kind: r for r in b}
    return [k for k in sorted(set(ka) & set(kb)) if not _alike(ka[k], kb[k])]


# ------------------------------------------------------------- inferring
def _key(session) -> Optional[tuple]:
    try:
        data = bytes(session.binfile.data)
    except Exception:
        return None
    return (getattr(session, "platform", "") or "", len(data),
            hashlib.sha1(data).hexdigest())


def _remember(cache, key, value):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _KEEP:
        cache.popitem(last=False)
    return value


def _fitted(kind, summary, parts, confident, why) -> object:
    return _hw.Fitted(id="maps-" + kind, kind=kind, summary=summary, parts=parts,
                      source=FROM_MAPS, extra={"confident": bool(confident), "why": why})


def _catalogue():
    from . import injectors as I
    try:
        from .gui_injectors import user_presets       # the user's own (Qt settings)
        user = user_presets()
    except Exception:
        user = []
    return I.catalogue(user)


def _pd(session) -> Optional[Dict[str, Optional[object]]]:
    from . import eoi as _eoi
    from . import injectors as I
    if I.availability(session) is not None:
        return None
    models = [m for m in _eoi.models(session) if not I._missing_duration(m)]
    if not models:
        return None
    out: Dict[str, Optional[object]] = {}
    cat = None
    for m in models:
        g = getattr(m, "group", "") or ""
        try:
            char = I.characteristic(m)
        except Exception:
            out[g] = None
            continue
        ck = hashlib.sha1(json.dumps(char.to_json(), sort_keys=True)
                          .encode("utf-8")).hexdigest()
        hit = _chars.get(ck)
        if hit is None:
            if cat is None:
                cat = _catalogue()
            ms = I.identify(char, cat, 1)
            t = ms[0] if ms else None
            hit = _remember(_chars, ck, (t.preset.id, t.preset.name, t.exact)
                            if t is not None and t.same else (None, "", False))
        pid, name, exact = hit
        out[g] = _fitted(_hw.PD_INJECTORS, "%s injectors" % name,
                         {"preset": pid}, exact,
                         "its duration maps are the %s preset's%s" % (
                             name, "" if exact else " within 1 % on average")) \
            if pid else None
    return out


def _vepump():
    try:
        from .modules import registry as _registry
        m = _registry.for_platform("edc15")
        if m is None or getattr(m, "variant", "full") != "full":
            return None
        from .edc15 import vepump15                      # the module's own
        return vepump15
    except Exception:
        return None


def _ve(session) -> Optional[Dict[str, Optional[object]]]:
    if (getattr(session, "platform", "") or "") != "edc15":
        return None
    V = _vepump()
    if V is None or not V.available(session):
        return None
    out: Dict[str, Optional[object]] = {}
    for g, idt in V.identify_all(session).items():
        if idt.ref is None or idt.kind not in ("factory", "scaled"):
            out[g] = None
            continue
        parts = {}
        if idt.plunger_mm:
            parts["plunger_mm"] = float(idt.plunger_mm)
        nz = V.identified_nozzle(idt)
        if nz is not None:
            parts["nozzle"] = nz.id
        out[g] = _fitted(_hw.VE_PUMP, V.hardware_text(parts), parts,
                         idt.kind == "factory", idt.text()) if parts else None
    return out


def infer_all(session) -> Dict[str, Optional[object]]:
    """``{dataset group: Fitted or None}``: what each dataset's maps look
    set up for ({} when the file has nothing to tell it by)."""
    key = _key(session)
    if key is None:
        return {}
    hit = _files.get(key)
    if hit is not None:
        _files.move_to_end(key)
        return hit
    got = None
    for fn in (_pd, _ve):
        try:
            got = fn(session)
        except Exception:                               # noqa: BLE001
            got = None
        if got is not None:
            break
    return _remember(_files, key, got or {})


def infer(session, group: Optional[str] = None) -> Optional[HardwareOf]:
    """What ``session``'s calibration looks set up for: dataset ``group``'s
    when it has one, else what every dataset agrees on. None when not told
    (no preset or factory map matches, the datasets disagree, a platform
    with nothing to compare)."""
    if session is None:
        return None
    per = infer_all(session)
    if not per:
        return None
    if group is not None and group in per:
        rec = per[group]
    else:
        vals = list(per.values())
        rec = vals[0]
        if rec is None or any(v is None or v.parts != rec.parts for v in vals[1:]):
            return None
        if any(not is_confident(v) for v in vals):
            rec = next(v for v in vals if not is_confident(v))
    return HardwareOf([rec]) if rec is not None else None


def forget() -> None:
    """Drop what was worked out (tests)."""
    _files.clear()
    _chars.clear()


__all__ = ["FROM_MAPS", "HardwareOf", "is_inferred", "is_confident", "inferred",
           "short", "describe", "differ", "infer", "infer_all", "forget"]

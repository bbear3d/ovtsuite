# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The full-load torque of an ECU kept in the same project -- what the shift
editor's "Full-load optimum" works from (:mod:`ovtsuite.gearbox_edit.fullload`).

A project holds the gearbox and the engine side by side. This picks an ECU
set and a version of it (by default the one recorded as flashed -- what is in
the car -- else the branch head), opens it the way a project tab does (its
packaged definition, resized tables and user parameters included), and runs
the calculated dyno (:mod:`ovtsuite.dyno`) on it, quantity turned into torque
through the set's ORIGINAL (as read), as the dyno chart does by default.

Qt-free.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import dyno as _dy
from .gearbox_edit.fullload import TorqueCurve

FLASHED, HEAD = "flashed", "head"


@dataclass
class Version:
    cid: str
    label: str
    kind: str          # FLASHED | HEAD | "checkpoint"


def ecu_sets(project) -> list:
    """The project's binary sets that are engine ECUs."""
    return [s for s in getattr(project, "sets", ()) if getattr(s, "kind", "") == "ecu"]


def versions(project, sid: str) -> List[Version]:
    """The versions of set ``sid`` worth working from: the one flashed (when
    recorded), the head of each branch, then every checkpoint, newest first."""
    s = project.set(sid)
    out: List[Version] = []
    seen = set()
    fl = (s.extra or {}).get("flashed") or {}
    cid = fl.get("checkpoint")
    if cid and s.checkpoint(cid) is not None:
        c = s.checkpoint(cid)
        out.append(Version(cid, "As flashed %s (%s)" % (
            (fl.get("ts") or "")[:10], c.label or c.kind), FLASHED))
        seen.add(cid)
    for b in s.branches:
        if b.head in seen:
            continue
        out.append(Version(b.head, "Branch head: %s" % b.name, HEAD))
        seen.add(b.head)
    for c in sorted(s.checkpoints, key=lambda c: c.ts or "", reverse=True):
        if c.id in seen:
            continue
        lab = "as read" if c.parent is None else (c.label or c.kind)
        out.append(Version(c.id, "%s %s" % ((c.ts or "")[:16].replace("T", " "), lab),
                           "checkpoint"))
    return out


def default_version(project, sid: str) -> Optional[Version]:
    v = versions(project, sid)
    return v[0] if v else None


def default_set(project):
    """The ECU set to start from: the one with a flash recorded most
    recently, else the first."""
    sets = ecu_sets(project)
    if not sets:
        return None
    flashed = [s for s in sets if (s.extra or {}).get("flashed")]
    if flashed:
        return max(flashed, key=lambda s: s.extra["flashed"].get("ts") or "")
    return sets[0]


def session_for(project, sid: str, cid: str):
    """Set ``sid`` at checkpoint ``cid`` as a session, opened as a project tab
    opens it."""
    from .project import sessionbridge as _sb
    from .session import TunerSession
    state = project.state(sid, cid)
    ext, blob = _sb.working_file(project, sid, state)
    fd, path = tempfile.mkstemp(suffix=ext or ".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        sess = TunerSession(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    for apply in (_sb.apply_packaged_definition,):
        try:
            apply(project, sid, sess)
        except Exception:
            pass
    from . import resize as _rz
    from . import userparams as _up
    for mod in (_rz, _up):
        try:
            mod.apply_from_project(project, sid, sess)
        except Exception:
            pass
    return sess


def root_of(project, sid: str, cid: str) -> Optional[str]:
    s = project.set(sid)
    r = s.root_of(cid)
    return r.id if r is not None else None


class EcuTorque:
    """Sessions and curves of the project's ECU sets, worked out once each."""

    def __init__(self, project):
        self.project = project
        self._sessions: Dict[Tuple[str, str], object] = {}

    def session(self, sid: str, cid: str):
        key = (sid, cid)
        if key not in self._sessions:
            self._sessions[key] = session_for(self.project, sid, cid)
        return self._sessions[key]

    def curves(self, sid: str, cid: str, vn_by_gear: Dict[int, float],
               cond: Optional[_dy.Conditions] = None, session=None,
               reference: bool = True):
        """``(curve, {gear: curve}, note)``: the any-gear curve, and each gear's
        own where the ECU limits that gear differently -- its driver's-wish
        map picked by v/n (``vn_by_gear``, (km/h)/rpm) and its gearbox torque
        limit. A gear whose curve matches the any-gear one is left out."""
        import dataclasses
        cond = cond or _dy.Conditions()
        base, note = self.curve(sid, cid, cond, session, reference)
        if base is None:
            return None, {}, note
        out: Dict[int, TorqueCurve] = {}
        lower = []
        for g, vn in sorted(vn_by_gear.items()):
            c = dataclasses.replace(cond, gear=g, v_per_n=vn)
            cg, _n = self.curve(sid, cid, c, session, reference)
            if cg is None:
                continue
            diff = max((abs(a - b) for a, b in zip(cg.torque, base.torque)), default=0.0)
            if diff > 1.0 or len(cg.torque) != len(base.torque):
                out[g] = cg
                pk = max(cg.torque) if cg.torque else 0.0
                lower.append("gear %d %.0f Nm" % (g, pk))
        if lower:
            note += "; by gear (driver's wish by v/n, gearbox limit): " + ", ".join(lower)
        return base, out, note

    def curve(self, sid: str, cid: str, cond: Optional[_dy.Conditions] = None,
              session=None, reference: bool = True) -> Tuple[Optional[TorqueCurve], str]:
        """``(curve, note)``: the full-load curve of ``sid`` at ``cid`` (or of
        ``session`` -- an open tab's unsaved state -- when given), through the
        set's original. ``curve`` is None when it can't be worked out; the
        note says why."""
        cond = cond or _dy.Conditions()
        sess = session if session is not None else self.session(sid, cid)
        ms = _dy.models(sess)
        if not ms:
            why = _dy.models_error(sess)
            return None, ("No torque model for this ECU%s." % (": " + why if why else
                          " (its module has no dyno calculation)"))
        m = ms[0]
        ref = rm = fm = None
        if reference:
            rid = root_of(self.project, sid, cid)
            if rid is not None and rid != cid:
                try:
                    ref = self.session(sid, rid)
                    rm = _dy.matching(_dy.models(ref), m.group)
                    fm = _dy.fuel_map(sess, ref, m.group, cond.coolant)
                except Exception:
                    ref = rm = fm = None
        c = _dy.calculate(m, cond, rm, fuel=fm)
        if not c.points:
            return None, " ".join(c.notes) or "The curve could not be worked out."
        pk = c.peak_power()
        pt = c.peak_torque()
        note = "%.0f kW at %.0f rpm, %.0f Nm at %.0f rpm (%s)" % (
            pk.power, pk.rpm, pt.torque, pt.rpm, c.torque_source or "own conversion")
        return TorqueCurve.from_points(c.points, label=c.label), note


def full_torque_pedal(model, vn_by_gear: Dict[int, float], pedals,
                      rpm_lo: float = 1500.0, rpm_hi: float = 3500.0,
                      cond: Optional[_dy.Conditions] = None,
                      at_least: float = 0.98):
    """``(pedal or None, {pedal: lowest fraction})``: the lowest of ``pedals``
    at which the ECU's driver's wish asks for full load (``at_least`` of what
    it delivers there) at every engine speed between ``rpm_lo`` and
    ``rpm_hi`` in every gear of ``vn_by_gear`` -- above it more pedal adds no
    torque, so a gearbox pedal row there is full load in engine terms.
    The fraction table holds, per pedal, the lowest demand found."""
    import dataclasses
    cond = cond or _dy.Conditions()
    gears = sorted(vn_by_gear.items()) or [(None, None)]
    rpms = [rpm_lo + (rpm_hi - rpm_lo) * i / 8.0 for i in range(9)]
    table = {}
    for p in sorted(pedals):
        low = 1.0
        for g, vn in gears:
            c = dataclasses.replace(cond, gear=g, v_per_n=vn)
            for n in rpms:
                f = _dy.demand_fraction(model, n, p, c)
                if f is None:
                    return None, {}
                low = min(low, f)
        table[p] = low
    hit = next((p for p in sorted(table) if table[p] >= at_least and p < 99.9), None)
    return hit, table

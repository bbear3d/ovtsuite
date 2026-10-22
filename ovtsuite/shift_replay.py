# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The project's logged drives, ready to replay against shift lines
(:mod:`ovtsuite.gearbox_edit.replay`).

A gearbox log says where the car was -- gear, output speed, pedal, road
speed, the program -- and the engine log taken at the same time says what
the engine gave. This pairs the two (by when each log started), turns the
engine's logged injection quantity into brake torque through the ECU's own
conversion (:meth:`ovtsuite.dyno.DynoModel.torque_of`), and supplies the
fuel model for the replay: the quantity that conversion needs for a brake
torque at an engine speed, as litres per hour. On Bear's project the
conversion's quantity matches the logged one to a median 0.4 %.

Qt-free.
"""
from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from . import dyno as _dy
from .gearbox_edit.replay import Sample

#: channel label patterns (the logger's own names), by role
TCU_CHANNELS = {"gear": r"Tcu_stGear|Engaged gear", "out": r"Tcu_nOut|Output shaft speed",
                "pedal": r"Tcu_rPed|Accelerator pedal", "speed": r"Tcu_vVeh|Vehicle speed",
                "program": r"Tcu_stProg|Current shift program", "dsp": r"Tcu_stDsp"}
ECU_CHANNELS = {"rpm": r"Eng_nAvrg|Engine Speed", "q": r"InjCtl_qSetUnBal|Injection Quantity",
                "trq": r"FMTC_trqInr"}
DIESEL_KG_PER_L = 0.835
PAIR_WITHIN_S = 10.0


def _load(project, att):
    from .datalog import logfile as _lf
    raw = project.log_bytes(att.id)
    fd, path = tempfile.mkstemp(suffix=os.path.splitext(att.filename)[1] or ".csv")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
        return _lf.load(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _start(log, filename: str) -> Optional[datetime]:
    st = getattr(log, "stamps", None)
    if st and st[0].year >= 2000:
        return st[0]
    m = re.search(r"(\d{8})_(\d{6})", filename or "")
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def _series(log, pat) -> List[Tuple[float, float]]:
    rx = re.compile(pat)
    for i, c in enumerate(log.channels):
        if rx.search(c.label or ""):
            return [(t, v) for t, v in zip(log.seconds(), log.column(i)) if v is not None]
    return []


class _Track:
    """Linear interpolation in one channel, for increasing times."""

    def __init__(self, ser):
        self.t = [a for a, _ in ser]
        self.v = [b for _, b in ser]
        self.k = 0

    def __bool__(self):
        return bool(self.t)

    def at(self, t: float) -> Optional[float]:
        T = self.t
        if not T or t < T[0] - 1.0 or t > T[-1] + 1.0:
            return None
        k = self.k
        while k + 1 < len(T) and T[k + 1] < t:
            k += 1
        while k > 0 and T[k] > t:
            k -= 1
        self.k = k
        if k + 1 >= len(T):
            return self.v[-1]
        a, b = T[k], T[k + 1]
        if t <= a:
            return self.v[k]
        f = (t - a) / (b - a) if b > a else 1.0
        return self.v[k] + (self.v[k + 1] - self.v[k]) * f


class EngineFuel:
    """Brake torque <-> quantity through one ECU model's conversion, and fuel
    per hour, memoised on a 10 rpm x 1 Nm grid."""

    def __init__(self, model, cond: Optional[_dy.Conditions] = None, cylinders: int = 0):
        self.m = model
        self.cond = cond or _dy.Conditions()
        _d, c = model.engine(self.cond)
        self.cyl = cylinders or c or 4
        self._q: Dict[tuple, float] = {}

    def torque(self, rpm: float, q: float) -> Optional[float]:
        r = self.m.torque_of(rpm, q, self.cond)
        return None if r is None else r[0]

    def _table(self, rpm: float):
        """``(torques, quantities)`` at ``rpm`` (25 rpm steps), q in 1 mg
        steps up to where the torque stops rising -- inverted by
        interpolation, so a whole replay costs a few thousand conversions."""
        key = int(round(rpm / 25.0))
        got = self._q.get(key)
        if got is None:
            n = key * 25.0
            ts, qs = [], []
            q = 0.0
            while q <= 160.0:
                t = self.torque(n, q)
                if t is None:
                    break
                if ts and t <= ts[-1]:
                    if q > 20.0:
                        break
                    q += 1.0
                    continue
                ts.append(t)
                qs.append(q)
                q += 1.0
            got = self._q[key] = (ts, qs)
        return got

    def quantity(self, rpm: float, torque: float) -> float:
        ts, qs = self._table(rpm)
        if not ts:
            return 0.0
        if torque <= ts[0]:
            return qs[0]
        if torque >= ts[-1]:
            return qs[-1] * torque / ts[-1] if ts[-1] > 0 else qs[-1]
        import bisect
        k = bisect.bisect_left(ts, torque)
        a, b = ts[k - 1], ts[k]
        return qs[k - 1] + (qs[k] - qs[k - 1]) * (torque - a) / (b - a)

    def litres_per_hour(self, torque: float, rpm: float) -> float:
        if torque <= 0:
            return 0.0
        q = self.quantity(rpm, torque)
        return q * rpm / 2.0 * self.cyl * 60.0 / 1e6 / DIESEL_KG_PER_L


def pairs(project, tcu_sid: str, ecu_sids) -> List[tuple]:
    """``[(tcu attachment, ecu attachment, ecu start - tcu start in s)]``:
    each gearbox log with the engine log that started within
    :data:`PAIR_WITHIN_S` of it."""
    logs = list(getattr(project, "logs", ()))
    tcu = [a for a in logs if a.set == tcu_sid]
    ecu = [a for a in logs if a.set in set(ecu_sids)]

    def when(a):
        m = re.search(r"(\d{8})_(\d{6})", a.filename or "")
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S") if m else None
        except ValueError:
            return None
    out = []
    used = set()
    for a in tcu:
        ta = when(a)
        if ta is None:
            continue
        best = None
        for b in ecu:
            tb = when(b)
            if tb is None or b.id in used:
                continue
            d = (tb - ta).total_seconds()
            if abs(d) <= PAIR_WITHIN_S and (best is None or abs(d) < abs(best[1])):
                best = (b, d)
        if best is not None:
            used.add(best[0].id)
            out.append((a, best[0], best[1]))
    return out


class Drives:
    """The paired logs of a project as replay samples, with what the engine
    did: ``samples``, the brake-torque ``envelope`` by 100 rpm (the most the
    logs show), and what was read."""

    def __init__(self, samples: List[Sample], envelope: Dict[int, float],
                 logs: int, skipped: List[str]):
        self.samples = samples
        self.envelope = envelope
        self.logs = logs
        self.skipped = skipped

    @property
    def hours(self) -> float:
        return sum(s.dt for s in self.samples) / 3600.0

    @property
    def km(self) -> float:
        return sum(s.v * s.dt for s in self.samples) / 3600.0


class Cancelled(Exception):
    """The caller's ``cancel()`` said stop."""


def drives(project, tcu_sid: str, ecu_sids, fuel: EngineFuel,
           progress: Optional[Callable[[str, int, int], None]] = None,
           cancel: Optional[Callable[[], bool]] = None, warm: bool = True) -> Drives:
    """Every paired gearbox + engine log of the project as :class:`Sample` s
    on the engine log's own sample times (the brake power is the engine's).

    ``progress(label, done, total)`` is told each step (two per log pair, then
    the fuel model); ``cancel()`` returning True stops with
    :class:`Cancelled`. ``warm`` builds the fuel model's tables for every
    engine speed the logs reach, so the first replay is as quick as the rest.
    """
    samples: List[Sample] = []
    env: Dict[int, float] = {}
    skipped: List[str] = []
    n = 0
    todo = pairs(project, tcu_sid, ecu_sids)
    total = 2 * len(todo) + (1 if warm else 0)
    step = 0

    def tell(label):
        if cancel is not None and cancel():
            raise Cancelled()
        if progress is not None:
            progress(label, step, total)
    for k, (ta, ea, off) in enumerate(todo):
        try:
            tell("Reading log %d of %d: %s" % (k + 1, len(todo), ta.filename))
            tl = _load(project, ta)
            step += 1
            tell("Reading log %d of %d: %s" % (k + 1, len(todo), ea.filename))
            el = _load(project, ea)
            step += 1
        except Cancelled:
            raise
        except Exception:
            skipped.append(ea.filename)
            continue
        T = {k: _Track(_series(tl, p)) for k, p in TCU_CHANNELS.items()}
        E = {k: _Track(_series(el, p)) for k, p in ECU_CHANNELS.items()}
        if not (T["gear"] and T["out"] and T["pedal"] and T["speed"] and E["rpm"]
                and (E["q"] or E["trq"])):
            skipped.append(ea.filename)
            continue
        times = E["q"].t if E["q"] else E["trq"].t
        prev = None
        got = 0
        for t in times:
            tt = t + off                  # the gearbox log's clock
            rpm = E["rpm"].at(t)
            if E["q"]:
                q = E["q"].at(t)
                trq = fuel.torque(rpm, q) if (rpm and q is not None) else None
            else:
                ti = E["trq"].at(t)
                fr = getattr(fuel.m, "friction", None)
                trq = (ti - fr(rpm, fuel.cond)) if (ti is not None and rpm and fr) else None
            vals = [T[k].at(tt) for k in ("speed", "pedal", "gear", "out")]
            if prev is not None and 0 < t - prev <= 2.0 and rpm and trq is not None \
                    and None not in vals:
                v, p, g, o = vals
                b = int(rpm // 100)
                env[b] = max(env.get(b, 0.0), trq)
                prog = T["program"].at(tt) if T["program"] else None
                dsp = T["dsp"].at(tt) if T["dsp"] else None
                samples.append(Sample(t - prev, v, p, int(round(g)), o, trq * rpm / 9549.2966,
                                      int(round(prog)) if prog is not None else None,
                                      int(round(dsp)) if dsp is not None else None))
                got += 1
            prev = t
        if got:
            n += 1
        else:
            skipped.append(ea.filename)
    if warm and env:
        tell("Preparing the fuel model")
        lo, hi = min(env) * 100, (max(env) + 1) * 100
        rpm = max(700.0, lo - 200.0)
        while rpm <= hi + 400.0:
            fuel.quantity(rpm, 100.0)
            rpm += 25.0
            if cancel is not None and cancel():
                raise Cancelled()
        step += 1
    if progress is not None:
        progress("Done", total, total)
    return Drives(samples, env, n, skipped)


def envelope_fn(curve, env: Dict[int, float]):
    """``tmax(rpm)``: the full-load curve, raised to any brake torque the logs
    show near that speed (the calculation assumes steady boost)."""
    def tmax(n):
        b = int(n // 100)
        base = curve.torque_at(n) if curve is not None else 0.0
        return max(base, env.get(b, 0.0))
    return tmax


__all__ = ["Cancelled", "TCU_CHANNELS", "ECU_CHANNELS", "EngineFuel", "pairs", "Drives", "drives",
           "envelope_fn"]

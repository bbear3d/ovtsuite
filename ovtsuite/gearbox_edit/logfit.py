# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Logged shifts and lock-ups against the schedules, and blend weights
fitted from them.

A module turns its logs into :class:`Event` s -- where a shift started, where
the converter locked -- each with the pedal, the output-shaft speed, the
program the gearbox reported and the calibration version that was in the car.
The editor plots them on the chart, and :func:`fit_weights` asks which mix of
candidate schedules explains them:

    observed = sum(w_k * schedule_k(line, pedal)) + lateness(kind)
    w_k >= 0, sum(w_k) = 1

``lateness`` is one free offset per direction: a logged shift is seen after
the threshold was crossed (the sample interval, the shift's own start-up), so
the absolute level is not evidence, only the shape across pedal and lines is.
The fit is reported, never forced: when the candidates are too alike at the
logged pedals the weights come with large uncertainties and the verdict says
they cannot be told apart from these logs.

Pure Python -- no numpy -- since the systems are a few unknowns.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .model import ScheduleSet, interp


@dataclass
class Event:
    kind: str                      # "up" | "down" (shifts) | "lock" | "release"
    line: Optional[int]            # Line.index the event is judged on
    gear: int                      # gear the event happened in (gear left, for a shift)
    pedal: float                   # %
    out_rpm: float                 # output-shaft rpm
    program: Optional[int] = None  # the gearbox's reported program
    dsp: Optional[int] = None      # dynamic shift program
    t: float = 0.0                 # seconds into the log
    log: str = ""                  # log file name
    version: Optional[str] = None  # calibration version (checkpoint) in the car
    note: str = ""
    #: Output-speed rate at the event, rpm/s: a shift is logged after the
    #: threshold was crossed, by roughly this rate x the gearbox's latency.
    rate: float = 0.0
    #: True when the pedal held still before the event, so the SPEED crossed
    #: the threshold. False when the pedal moved the threshold past the speed
    #: (a kick or a lift): then the event only bounds the threshold, from
    #: above for an upshift and from below for a downshift.
    steady: bool = True

    def label(self) -> str:
        bits = ["%s %s" % (self.log, _clock(self.t))]
        if self.program is not None:
            bits.append("program %d" % self.program)
        if self.dsp is not None:
            bits.append("dsp %d" % self.dsp)
        if self.version:
            bits.append("flashed %s" % self.version[:10])
        return ", ".join(bits)


def _clock(t: float) -> str:
    m, s = divmod(int(round(t)), 60)
    return "%d:%02d" % (m, s)


#: ``(schedule index, line, pedal, version) -> output rpm`` -- a schedule's
#: value as the car had it (the version flashed when the log was taken).
Lookup = Callable[[int, int, float, Optional[str]], Optional[float]]


def lookup_from(sset: ScheduleSet, grids: Dict[Optional[str], Dict[int, Tuple[Sequence[float], List[float]]]]) -> Lookup:
    """A :data:`Lookup` over ``{version: {schedule index: (pedal, values)}}``;
    the ``None`` entry (the file as it is now) stands in for a version that
    could not be read."""
    def look(i, line, p, version):
        g = grids.get(version) or grids.get(None) or {}
        got = g.get(i)
        if got is None:
            return None
        pedal, vals = got
        s = sset.schedules[i]
        if line >= s.cols:
            return None
        col = [vals[r * s.cols + line] for r in range(len(pedal))]
        if any(s.is_off(v) for v in col):
            return None
        return interp(pedal, col, p)
    return look


@dataclass
class Fit:
    weights: Dict[int, float]                  # schedule index -> weight
    errors: Dict[int, float]                   # schedule index -> 1-sigma (by log)
    lateness: Dict[str, float]                 # "up" / "down" -> output rpm
    latency: float                             # seconds x output-speed rate
    events: int                                # steady events fitted
    logs: int                                  # logs they came from
    rms: float
    determinable: bool
    verdict: str
    bounds_ok: int = 0                         # pedal-driven events consistent
    bounds: int = 0                            # pedal-driven events judged
    used: List[Event] = field(default_factory=list)
    residuals: List[float] = field(default_factory=list)


def _solve(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
    """Gaussian elimination with partial pivoting; None when singular."""
    n = len(A)
    M = [list(A[i]) + [b[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[p][c]) < 1e-9:
            return None
        M[c], M[p] = M[p], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                if f:
                    for k in range(c, n + 1):
                        M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def _inverse(A: List[List[float]]) -> Optional[List[List[float]]]:
    n = len(A)
    cols = []
    for j in range(n):
        e = [1.0 if i == j else 0.0 for i in range(n)]
        x = _solve(A, e)
        if x is None:
            return None
        cols.append(x)
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def _lsq(X: List[List[float]], y: List[float]):
    """Least squares: ``(coefficients, sse, (X'X)^-1)`` or None."""
    k = len(X[0]) if X else 0
    if not k or len(X) < k:
        return None
    XtX = [[sum(r[a] * r[b] for r in X) for b in range(k)] for a in range(k)]
    Xty = [sum(r[a] * v for r, v in zip(X, y)) for a in range(k)]
    beta = _solve(XtX, Xty)
    if beta is None:
        return None
    sse = sum((v - sum(c * x for c, x in zip(beta, r))) ** 2 for r, v in zip(X, y))
    return beta, sse, _inverse(XtX)


def _best(rows, n, kinds, max_corners):
    """Best feasible subset fit over ``rows`` ``[(event, [T_k])]``."""
    best = None
    for size in range(1, min(n, max_corners) + 1):
        for sub in itertools.combinations(range(n), size):
            # w_last = 1 - sum(others):
            #   y - T_last = sum w_j (T_j - T_last) + latency * rate + b_kind
            last = sub[-1]
            X, y = [], []
            for e, ts in rows:
                X.append([ts[j] - ts[last] for j in sub[:-1]] + [e.rate]
                         + [1.0 if e.kind == k else 0.0 for k in kinds])
                y.append(e.out_rpm - ts[last])
            got = _lsq(X, y)
            if got is None:
                continue
            beta, sse, _inv = got
            w = list(beta[:size - 1])
            w.append(1.0 - sum(w))
            if min(w) < -1e-6:
                continue
            if best is None or sse < best[1] - 1e-6:
                best = (sub, sse, beta, w)
    return best


def fit_weights(sset: ScheduleSet, events: Sequence[Event], candidates: Sequence[int],
                lookup: Lookup, max_corners: int = 4,
                max_error: float = 0.15, tolerance: float = 64.0) -> Optional[Fit]:
    """Non-negative weights summing to 1 over ``candidates`` that best explain
    the shift ``events``.

    Only *steady* events (the speed crossed the threshold) are fitted, by
    least squares with a latency term (seconds x the logged output-speed
    rate) and one offset per direction. Every subset of up to
    ``max_corners`` candidates is tried and the best feasible one wins.

    The weights' errors are a jackknife over the LOGS -- refit leaving each
    log out -- because events from one drive share its conditions and are not
    independent. ``determinable`` is False when there are too few events or
    logs, or a weight moves by more than ``max_error``.

    Pedal-driven events (the pedal moved the threshold past the speed) only
    bound it -- an upshift's threshold is at or below the logged speed, a
    downshift's at or above -- and are counted as consistent or not, within
    ``tolerance`` output rpm.
    """
    rows, bound_rows = [], []
    for e in events:
        if e.kind not in ("up", "down") or e.line is None:
            continue
        ts = [lookup(i, e.line, e.pedal, e.version) for i in candidates]
        if any(t is None for t in ts):
            continue
        (rows if e.steady else bound_rows).append((e, ts))
    if not rows:
        return None
    kinds = sorted({e.kind for e, _ in rows})
    n = len(candidates)
    best = _best(rows, n, kinds, max_corners)
    if best is None:
        return None
    sub, sse, beta, w = best
    weights = {candidates[ci]: max(0.0, wv) for ci, wv in zip(sub, w)}
    k0 = len(sub) - 1
    latency = beta[k0]
    lateness = {k: beta[k0 + 1 + j] for j, k in enumerate(kinds)}

    # jackknife over logs, on the whole candidate list
    logs = sorted({e.log for e, _ in rows})
    errors: Dict[int, float] = {}
    if len(logs) >= 3:
        reps = []
        for lg in logs:
            part = [r for r in rows if r[0].log != lg]
            got = _best(part, n, sorted({e.kind for e, _ in part}), max_corners) if part else None
            if got is None:
                continue
            reps.append({candidates[ci]: wv for ci, wv in zip(got[0], got[3])})
        m = len(reps)
        if m >= 3:
            for i in candidates:
                vals = [r.get(i, 0.0) for r in reps]
                mean = sum(vals) / m
                errors[i] = math.sqrt((m - 1) / m * sum((v - mean) ** 2 for v in vals))
    resid = []
    for e, ts in rows:
        pred = sum(weights.get(candidates[j], 0.0) * ts[j] for j in range(n)) \
            + latency * e.rate + lateness.get(e.kind, 0.0)
        resid.append(e.out_rpm - pred)
    N, p = len(rows), len(beta)
    rms = math.sqrt(sse / N)
    ok_b = 0
    for e, ts in bound_rows:
        thr = sum(weights.get(candidates[j], 0.0) * ts[j] for j in range(n))
        ok_b += (thr <= e.out_rpm + tolerance) if e.kind == "up" else (thr >= e.out_rpm - tolerance)
    worst = max(errors.values()) if errors else float("inf")
    ok = N >= 5 * p and len(logs) >= 3 and worst <= max_error
    if N < 5 * p:
        verdict = ("too few steady events (%d for %d unknowns) -- not determinable "
                   "from these logs" % (N, p))
    elif len(logs) < 3 or not errors:
        verdict = "fewer than three logs: no error estimate -- not determinable from these logs"
    elif worst > max_error:
        verdict = ("the weights move by up to \u00b1%.2f from log to log -- the candidates are "
                   "too alike at the logged pedals to tell apart; not determinable from "
                   "these logs" % worst)
    else:
        verdict = "determinable: weights hold to within \u00b1%.2f from log to log" % worst
    return Fit(weights, errors, lateness, latency, N, len(logs), rms, ok, verdict,
               ok_b, len(bound_rows), [e for e, _ in rows], resid)


def evaluate(sset: ScheduleSet, events: Sequence[Event], corners: Sequence[Tuple[int, float]],
             lookup: Lookup, tolerance: float = 64.0) -> Optional[Tuple[float, int, int, int]]:
    """How well fixed weights explain the steady events: ``(rms, events,
    pedal-driven consistent, pedal-driven judged)`` with the latency and the
    per-direction offsets fitted, as in :func:`fit_weights`. Weights are
    normalised to sum to 1."""
    total = sum(w for _, w in corners) or 1.0
    rows, bounds = [], []
    for e in events:
        if e.kind not in ("up", "down") or e.line is None:
            continue
        ts = [lookup(i, e.line, e.pedal, e.version) for i, _ in corners]
        if not ts or any(t is None for t in ts):
            continue
        thr = sum(t * w / total for t, (_, w) in zip(ts, corners))
        (rows if e.steady else bounds).append((e, thr))
    if not rows:
        return None
    kinds = sorted({e.kind for e, _ in rows})
    X = [[e.rate] + [1.0 if e.kind == k else 0.0 for k in kinds] for e, _ in rows]
    y = [e.out_rpm - thr for e, thr in rows]
    got = _lsq(X, y)
    if got is None:
        return None
    ok = sum(1 for e, thr in bounds
             if (thr <= e.out_rpm + tolerance if e.kind == "up" else thr >= e.out_rpm - tolerance))
    return math.sqrt(got[1] / len(rows)), len(rows), ok, len(bounds)

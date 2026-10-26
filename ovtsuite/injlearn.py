# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""What new unit injectors or nozzles really deliver, learned from data logs
(Tools > Injector setup > From logs; the AI tool ``injector_learn_report``).

The ECU never measures fuel, so the delivered quantity comes from one of two
places, and each gives a *delivery factor* k = delivered / requested at the
logged engine speed and requested quantity:

* **The lambda probe** (EDC16; the stock measuring block 40 carries
  ``LSU_rLmbd``): delivered = air per stroke / (14.5 x lambda). Used where the
  probe reads well (lambda 1.05-2.5: mid and full load, the smoke limit),
  warm and steady.
* **Same work, before and after** (both platforms, no probe): at the same
  steady operating point -- idle in neutral, the same road speed in the same
  gear -- the engine needs the same fuel whatever injector is fitted. The
  median quantity requested BEFORE the change (old hardware, its own correct
  calibration) is what the new hardware delivers at the quantity requested
  AFTER: k = q_before / q_after. Idle and part load, where the probe is weak.

The factors are averaged onto the duration maps' own breakpoints (a
triangular kernel: 400 rpm, 6 mg); a breakpoint with too little weight is
left as it is and listed. The correction makes the hardware deliver what is
asked: duration(q) becomes the current duration at q / k, held to a step per
pass, kept monotonic in quantity, and written through
:func:`ovtsuite.injectors.plan_fit` (one undo step, the start of injection by
the chosen policy -- the centre kept by default).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import injectors as I

STOICH = 14.5            # diesel, kg air per kg fuel


@dataclass
class LearnOptions:
    warm: float = 70.0              # deg C coolant
    lambda_lo: float = 1.05
    lambda_hi: float = 2.5
    min_q: float = 4.0              # mg/stroke: below it the factor is noise
    steady_rpm: float = 60.0        # max rpm change across neighbouring rows
    steady_q: float = 2.0           # max quantity change across neighbouring rows
    k_lo: float = 0.5               # plausible delivery factors
    k_hi: float = 2.0
    min_weight: float = 3.0         # per breakpoint
    max_step: float = 0.15          # largest correction per pass (factor)
    min_rows: int = 5               # per same-work point, each side
    rpm_kernel: float = 400.0
    q_kernel: float = 6.0


@dataclass
class Sample:
    rpm: float
    q: float
    k: float
    source: str                     # "lambda" | "same-work"
    weight: float = 1.0
    label: str = ""


@dataclass
class LearnReport:
    samples: List[Sample] = field(default_factory=list)
    points: List[Tuple[float, float, float, float]] = field(default_factory=list)
    plan: Optional[I.FitPlan] = None
    notes: List[str] = field(default_factory=list)
    used: Dict[str, int] = field(default_factory=dict)
    unmeasured: int = 0
    changed: int = 0

    def to_json(self) -> dict:
        p = self.plan
        return {
            "samples": {k: v for k, v in self.used.items()},
            "same_work_points": [
                {"point": s.label, "rpm": round(s.rpm), "quantity_after": round(s.q, 2),
                 "delivery_factor": round(s.k, 3)}
                for s in self.samples if s.source == "same-work"],
            "breakpoints_corrected": self.changed,
            "breakpoints_unmeasured": self.unmeasured,
            "factors": [{"rpm": round(n), "quantity": round(q, 1), "factor": round(k, 3),
                         "weight": round(w, 1)} for n, q, k, w in self.points],
            "maps": [{"map": c.entity.name, "what": c.label, "cells": c.cells,
                      "largest_change": round(c.max_delta, 3)} for c in (p.changes if p else [])],
            "findings": [{"level": f.level, "text": f.text} for f in (p.findings if p else [])],
            "notes": list(self.notes),
        }


# ------------------------------------------------------------------ logs
def _runs(session, paths):
    from .datalog.channels import ChannelNamer
    from .logtune.samples import runs_from_paths
    return runs_from_paths(list(paths), ChannelNamer(session)) if paths else []


def read_logs(session, paths) -> List[Tuple[str, str, object]]:
    """``(path, the ECU ident the log records, its run)`` for each log, read
    once -- so each dataset of a file can learn from them without reading
    them again. A VCDS-layout log records the ident the ECU sent on connect;
    the native layout records none ("")."""
    if not paths:
        return []
    import os
    from .datalog import logfile as LF
    from .datalog.channels import ChannelNamer
    from .logtune.samples import run_from_log
    namer = ChannelNamer(session)
    out = []
    for p in paths:
        log = LF.load(p)
        out.append((p, (log.identity or "").strip(),
                    run_from_log(log, namer, os.path.basename(p))))
    return out


def datasets_for_identity(session, identity: str) -> List[str]:
    """Every dataset whose ident the ECU's ident starts with (whitespace
    removed: KW1281 may split a word across blocks), the longest match only.
    Datasets that differ by coding alone share an ident (an EDC15's
    "Manual 1" and "Manual 2" are both "... SG ..."), so the ident narrows a
    log to them and cannot tell them apart. [] when nothing matches."""
    have = "".join((identity or "").split())
    if not have:
        return []
    best, out = 0, []
    for ds in getattr(session, "datasets", None) or []:
        sq = "".join((getattr(ds, "ident", "") or "").split())
        if not sq or not have.startswith(sq) or len(sq) < best:
            continue
        if len(sq) > best:
            best, out = len(sq), []
        out.append(ds.label)
    return out


def log_datasets(session, labels: Sequence[str], identities: Sequence[str]):
    """Which of ``labels`` each log applies to: ``[(labels, how)]``, ``how``
    "ident" (the log's recorded ident names those datasets) or "unknown"
    (no ident, or one that names none of them: every dataset, as Tools >
    Injector setup takes every log for every ticked dataset)."""
    out = []
    for ident in identities:
        hit = [x for x in datasets_for_identity(session, ident) if x in labels]
        out.append((hit, "ident") if hit else (list(labels), "unknown"))
    return out


def _steady(run, i, opts) -> bool:
    if i <= 0 or i >= len(run) - 1:
        return False
    for role, lim in (("rpm", opts.steady_rpm), ("iq", opts.steady_q)):
        a, b = run.get(role, i - 1), run.get(role, i + 1)
        if a is None or b is None or abs(b - a) > lim:
            return False
    return True


def _warm(run, i, opts) -> bool:
    c = run.get("clt", i)
    return c is None or c >= opts.warm      # no coolant channel: trust the user


def lambda_samples(runs, opts: LearnOptions) -> List[Sample]:
    out = []
    for run in runs:
        if not (run.has("lambda") and run.has("maf") and run.has("iq") and run.has("rpm")):
            continue
        for i in range(len(run)):
            lam, air = run.get("lambda", i), run.get("maf", i)
            q, n = run.get("iq", i), run.get("rpm", i)
            if None in (lam, air, q, n) or q < opts.min_q:
                continue
            if not (opts.lambda_lo <= lam <= opts.lambda_hi):
                continue
            if not (_warm(run, i, opts) and _steady(run, i, opts)):
                continue
            k = air / (STOICH * lam) / q
            if opts.k_lo <= k <= opts.k_hi:
                out.append(Sample(n, q, k, "lambda"))
    return out


def _point_key(run, i):
    """An operating point both logs can share: idle in neutral, or a road
    speed (5 km/h bins) in a gear."""
    v, n = run.get("vss", i), run.get("rpm", i)
    ped, gear = run.get("pedal", i), run.get("gear", i)
    if n is None:
        return None
    if (v is not None and v < 2.0) or (v is None and n < 1100 and (ped or 0) < 2):
        if ped is not None and ped > 2:
            return None
        return ("idle",)
    if v is None:
        return None
    return ("road", int(round(v / 5.0)), int(gear) if gear is not None else None)


def _points(runs, opts) -> Dict[tuple, List[Tuple[float, float]]]:
    pts: Dict[tuple, List[Tuple[float, float]]] = {}
    for run in runs:
        if not (run.has("iq") and run.has("rpm")):
            continue
        for i in range(len(run)):
            q, n = run.get("iq", i), run.get("rpm", i)
            if q is None or n is None or q < opts.min_q:
                continue
            if not (_warm(run, i, opts) and _steady(run, i, opts)):
                continue
            key = _point_key(run, i)
            if key is not None:
                pts.setdefault(key, []).append((n, q))
    return pts


def same_work_samples(before, after, opts: LearnOptions) -> Tuple[List[Sample], List[str]]:
    b, a = _points(before, opts), _points(after, opts)
    out, notes = [], []
    for key in sorted(set(b) & set(a), key=str):
        rb, ra = b[key], a[key]
        if len(rb) < opts.min_rows or len(ra) < opts.min_rows:
            continue
        nb = statistics.median(n for n, _q in rb)
        na = statistics.median(n for n, _q in ra)
        if abs(nb - na) > 150:              # not the same point (another gear)
            continue
        qb = statistics.median(q for _n, q in rb)
        qa = statistics.median(q for _n, q in ra)
        if qa <= 0:
            continue
        k = qb / qa
        if not (opts.k_lo <= k <= opts.k_hi):
            continue
        label = "idle" if key[0] == "idle" else "%d km/h%s" % (
            key[1] * 5, (" in gear %d" % key[2]) if key[2] is not None else "")
        out.append(Sample(na, qa, k, "same-work", min(len(rb), len(ra)) / 2.0, label))
    if before and not out:
        notes.append("No operating point is steady in both the before and the after "
                     "logs (idle in neutral, or the same road speed in the same gear, "
                     "at least %d rows each)." % opts.min_rows)
    return out, notes


# ------------------------------------------------------------------ fitting
def factor_at(samples: Sequence[Sample], rpm: float, q: float,
              opts: LearnOptions) -> Tuple[Optional[float], float]:
    sw = swk = 0.0
    for s in samples:
        wr = 1.0 - abs(s.rpm - rpm) / opts.rpm_kernel
        wq = 1.0 - abs(s.q - q) / opts.q_kernel
        if wr <= 0 or wq <= 0:
            continue
        w = wr * wq * s.weight
        sw += w
        swk += w * s.k
    if sw < opts.min_weight:
        return None, sw
    return swk / sw, sw


def learn(session, model, after: Sequence[str], before: Sequence[str] = (),
          opts: Optional[LearnOptions] = None, timing: str = I.KEEP_CENTRE,
          runs_after=None, runs_before=None) -> LearnReport:
    """Read the logs, work out the delivery factors and the plan that
    corrects ``model``'s duration set (nothing is written)."""
    opts = opts or LearnOptions()
    rep = LearnReport()
    ra = runs_after if runs_after is not None else _runs(session, after)
    rb = runs_before if runs_before is not None else _runs(session, before)
    if not ra:
        raise ValueError("no log taken with the new hardware")
    lam = lambda_samples(ra, opts)
    sw, notes = same_work_samples(rb, ra, opts) if rb else ([], [])
    rep.notes += notes
    rep.samples = lam + sw
    rep.used = {"lambda_rows": len(lam), "same_work_points": len(sw)}
    if not any(r.has("lambda") for r in ra):
        rep.notes.append("The logs carry no lambda channel: only the before/after "
                         "points can be used%s." % (
                             " (EDC16: log measuring block 40 for the ECU's own probe)"
                             if getattr(session, "platform", "") == "edc16" else ""))
    if not rep.samples:
        rep.notes.append("Nothing in the logs measures the delivered fuel: nothing to correct.")
        return rep

    char = I.characteristic(model)
    tables = []
    for t in char.tables:
        z = [row[:] for row in t.z]
        for i, n in enumerate(t.rpm):
            for j, q in enumerate(t.q):
                k, w = factor_at(rep.samples, n, q, opts)
                if k is None:
                    rep.unmeasured += 1
                    continue
                k = min(max(k, 1.0 - opts.max_step), 1.0 + opts.max_step)
                rep.points.append((n, q, k, w))
                if abs(k - 1.0) < 0.005:
                    continue
                z[i][j] = t.at(n, q / k)
                rep.changed += 1
        for i in range(len(t.rpm)):         # monotonic in quantity
            for j in range(1, len(t.q)):
                if z[i][j] < z[i][j - 1]:
                    z[i][j] = z[i][j - 1]
        tables.append(I.RawTable(t.rpm, t.q, z))
    new = I.Characteristic(char.sel_x, char.sel_y, tables, char.max_index)
    rep.plan = I.plan_fit(session, model, new,
                          I.FitOptions(body=False, timing=timing, selector=True),
                          label="Injector setup: duration from logs")
    if rep.changed and max(abs(k - 1) for _n, _q, k, _w in rep.points) >= opts.max_step - 1e-9:
        rep.notes.append("Some corrections were held to %d %% for this pass: apply, log "
                         "again and repeat." % round(opts.max_step * 100))
    return rep


__all__ = ["LearnOptions", "Sample", "LearnReport", "lambda_samples",
           "same_work_samples", "factor_at", "learn", "read_logs", "datasets_for_identity",
           "log_datasets", "STOICH"]

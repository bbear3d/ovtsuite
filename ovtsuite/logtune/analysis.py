# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Log-driven analysis and correction of an EDC16 PD calibration.

Reads data logs against the loaded calibration and produces two things:

* **findings** -- what the logs (and the calibration's own switches and axes)
  say about boost control, full-load fuelling, the active torque limiter,
  injection timing, air/EGR tracking and cruise fuel use;
* a **plan** (:class:`ovtsuite.autotune.Plan`) of bounded cell corrections,
  validated before anything touches the image and applied as one undo step.

Only two things are ever *written* automatically, because only these two have
a closed-loop measurement in the logs that says what the right value is:

``PCR_rBPCtlBas*_MAP`` -- the boost actuator pilot (feed-forward) map.
    Where the boost controller has settled (actual within a few tens of hPa of
    the setpoint for a sustained window) the duty it is outputting IS the duty
    that operating point needs; the pilot cell is moved part of the way toward
    it, so the PID has less to correct and overshoot/undershoot shrink.

The smoke limiter -- ``FlMng_qAFSCDSmoke_MAP`` (quantity mode) or the lambda
    maps ``FlMng_rLmbdSmkLim*_MAP`` (lambda mode), whichever the calibration's
    switches make live.
    Lambda is computed from the logged air mass and injected quantity
    (``lambda = m_air / (q_inj * 14.5)``). Every visited cell that would allow
    richer than the target is brought down to ``air / (14.5 x target)`` --
    the limiter's job is to never allow more than that, whichever limiter set
    the fuel on the day. A cell is only opened up where the logs show the smoke
    limit actually capping fuel, a bounded step per pass, and never past the
    floor.

Everything else -- start of injection, duration, torque limits, driver's wish,
torque/quantity conversion -- is reported, never auto-written: none of them has
a logged quantity that says what the correct value is (that needs EGT or
cylinder pressure, which this engine does not have), and the torque model is
also what the 6HP gearbox is told over CAN.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..autotune.plan import ERROR, NOTE, WARNING, Plan, Problem
from ..autotune.validate import COMMON_CHECKS
from . import grid
from .roles import RECIPES, ROLE_LABELS
from .samples import Run

SOI_HARD_MAX = 30.0      # deg BTDC -- no option moves a planned SOI past this
STOICH = 14.5            # diesel stoichiometric air/fuel ratio (by mass)
DIESEL_KG_PER_L = 0.832

CRITICAL = "critical"
WARN = "warning"
INFO = "info"
GOOD = "ok"
_SEV_RANK = {CRITICAL: 0, WARN: 1, INFO: 2, GOOD: 3}

PILOT_MAPS = ("PCR_rBPCtlBas_MAP", "PCR_rBPCtlBas2_MAP")
TARGET_MAPS = ("PCR_pBDesBas_MAP", "PCR_pBDesBas2_MAP")
SMOKE_Q_MAP = "FlMng_qAFSCDSmoke_MAP"
PRES_SMOKE_MAP = "FlMng_qPresSmoke_MAP"   # quantity limit over boost pressure
DURATION_MAPS = tuple("InjVlv_phiInjMI1_MAP%d" % i for i in range(7))


@dataclass
class Options:
    lambda_target: float = 1.20       # full-load smoke target
    lambda_floor: float = 1.12        # never plan anything richer than this
    wot_pedal: float = 90.0           # % pedal counted as full load
    warm_clt: float = 70.0            # deg C; the warm tune is learned from rows at or above this
    tune_warmup: bool = True          # colder rows correct the maps the ECU reads only while warming up
    warmup_min_clt: float = -40.0     # deg C; rows colder than this are ignored altogether
    steady_err: float = 60.0          # hPa -- "controller has settled"
    steady_time: float = 0.6          # s the settle must last
    pilot_gain: float = 0.6           # fraction of the learned offset applied per pass
    pilot_max_step: float = 4.0       # % duty per cell per pass
    pilot_min_weight: float = 3.0     # sample-equivalents a cell needs
    overshoot_trim_at: float = 120.0  # hPa overshoot that earns a pilot trim
    overshoot_trim: float = 2.0       # % duty removed ahead of an overshoot
    smoke_max_step: float = 0.08      # fraction a smoke cell may RISE per pass
    smoke_id_tol: float = 1.5         # mg: a smoke table "is" the live one within this median error
    smoke_min_weight: float = 1.5
    maf_settle_s: float = 0.4         # s after a pedal step whose air-mass reading still lags
    tip_in_step: float = 10.0         # % pedal rise that counts as a step
    eoi_max: float = 10.0             # deg ATDC end of injection worth flagging
    correct_pilot: bool = True
    correct_smoke: bool = True
    egt_warn: Optional[float] = None  # deg C pre-turbine, sustained; None = from EngPrt_facTempPreTrbn_MAP
    egt_crit: Optional[float] = None
    correct_soi: bool = False         # advance SOI where end of injection is late (opt-in)
    eoi_target: float = 8.0           # deg ATDC the SOI correction aims for
    soi_step: float = 1.0             # deg per cell per pass
    soi_max: float = 28.0             # absolute ceiling, deg BTDC
    soi_min_iq: float = 30.0          # only load cells get log-driven SOI changes
    soi_cruise_trial: float = 0.0     # deg advance in the cruise cells, for an A/B economy test
    soi_trial_max_iq: float = 25.0    # upper injected-quantity edge of those cruise cells, mg
    cylinders: int = 4


@dataclass
class Finding:
    severity: str
    topic: str
    title: str
    detail: str = ""
    table: Optional[Tuple[List[str], List[List[str]]]] = None


@dataclass
class CellNote:
    """Why a planned cell got the value it did (for the review table)."""

    map: str
    row: int
    col: int
    x: float
    y: float
    old: float
    new: float
    weight: float
    reason: str


@dataclass
class Result:
    findings: List[Finding] = field(default_factory=list)
    plan: Optional[Plan] = None
    cells: List[CellNote] = field(default_factory=list)
    coverage: Dict[str, List[str]] = field(default_factory=dict)   # recipe -> missing roles
    boost_model: str = ""

    def add(self, *a, **k):
        self.findings.append(Finding(*a, **k))

    def sorted_findings(self) -> List[Finding]:
        return sorted(self.findings, key=lambda f: _SEV_RANK.get(f.severity, 9))

    @property
    def worst(self) -> str:
        return min((f.severity for f in self.findings), key=lambda s: _SEV_RANK.get(s, 9),
                   default=GOOD)


def _fmt(v, nd=0):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "-"
    return ("%%.%df" % nd) % v


class LogTuner:
    def __init__(self, session, runs: Sequence[Run], options: Options = None,
                 baseline: Sequence[Run] = ()):
        self.s = session
        self.runs = list(runs)
        self.base = list(baseline)
        self.o = options or Options()
        self.E = {e.name: e for e in session.entities}
        self.res = Result(plan=Plan(session, "Tune from logs"))
        self._boost_w = None      # per-row map weights for the boost maps
        self._warm_cache = {}     # selector -> names of the maps a warm engine reads

    # ------------------------------------------------------------ helpers
    def ent(self, name, kinds=("map", "curve", "constant")):
        e = self.E.get(name)
        return e if e is not None and e.kind in kinds else None

    def const(self, name, default=None):
        e = self.ent(name, ("constant",))
        try:
            return e.cell(0, 0) if e is not None else default
        except Exception:
            return default

    def curve_at(self, name, x, default=None):
        e = self.ent(name, ("curve",))
        return grid.lookup(e, x) if e is not None else default

    def _warm(self, run, i):
        c = run.get("clt", i)
        return c is None or c >= self.o.warm_clt

    def _wot(self, run, i):
        p = run.get("pedal", i)
        return p is not None and p >= self.o.wot_pedal

    def _asks_more(self, run, i):
        """The driver asks for more torque than the limit allows -- the row is
        on a limiter whatever the pedal says (a 60 % take-off counts)."""
        drv, fin = run.get("trq_drv", i), run.get("trq_final", i)
        return drv is not None and fin is not None and fin > 1.0 and \
            drv > fin + max(5.0, 0.03 * fin)

    def _limited(self, run, i):
        return self._wot(run, i) or self._asks_more(run, i)

    def _clt_ok(self, run, i):
        """Row is inside the coolant range analysed at all (warm-up included)."""
        c = run.get("clt", i)
        if c is None or c >= self.o.warm_clt:
            return True
        return self.o.tune_warmup and c >= self.o.warmup_min_clt

    def _cold(self, run, i):
        c = run.get("clt", i)
        return c is not None and c < self.o.warm_clt

    def _rows(self, run: Run, roles, warm=True):
        """Rows with every role present. ``warm=True``: warm rows only;
        ``warm=False``: warm-up rows too (down to ``warmup_min_clt``)."""
        for i in range(len(run)):
            vals = {}
            ok = True
            for r in roles:
                v = run.get(r, i)
                if v is None:
                    ok = False
                    break
                vals[r] = v
            if ok and (self._warm(run, i) if warm else self._clt_ok(run, i)):
                yield i, vals

    def _segments(self, run: Run, pred, min_time: float):
        """Index ranges [a, b] where ``pred(i)`` holds for at least ``min_time`` s."""
        out, start = [], None
        n = len(run)
        for i in range(n + 1):
            ok = i < n and pred(i)
            if ok and start is None:
                start = i
            elif not ok and start is not None:
                if run.t[i - 1] - run.t[start] >= min_time:
                    out.append((start, i - 1))
                start = None
        return out

    def _any(self, role):
        return any(r.has(role) for r in self.runs)

    # ------------------------------------------------------------ entry
    def analyse(self) -> Result:
        self._static()
        if self.runs:
            self._coverage()
            self._boost_target()
            self._boost_quality()
            if self.o.correct_pilot:
                self._pilot()
            self._fuel()
            self._smoke()
            self._egt()
            self._timing()
            self._warmup()
            if self.o.correct_soi:
                self._soi()
            self._air()
            self._cruise()
        else:
            self.res.add(INFO, "logs", "No logs loaded",
                         "Only the calibration-only checks ran. Load logs recorded with "
                         "the presets in Data logging to analyse and correct the tune.")
        self._soi_trial()
        self.res.plan.validate(list(COMMON_CHECKS) + self._checks())
        for p in self.res.plan.problems:
            if p.severity == ERROR:
                self.res.add(CRITICAL, "plan", "Planned change blocked", str(p))
        return self.res

    # ------------------------------------------------------------ static
    def smoke_mode(self):
        """(mode, live table names, disabled?) from the calibration's switches.

        ``FlMng_swtqLimSmkQ_C`` picks lambda (0) or quantity (1) limiting and
        ``FlMng_swtSmokeSpace_C`` a single map (0) or the map *space* (1).
        Q=1/space=0 (every P475 OEM file, 038906016Q) uses FlMng_qAFSCDSmoke_MAP.
        Q=1/space=1 is proven only on P350: 038997016N/T and the BHW stock file
        zero FlMng_qAFSCDSmoke_MAP and the 2x2 FlMng_qAFSCDtEngSmoke*_MAP and
        calibrate only FlMng_rLmbdSmkLim0..2_MAP (plus FlMng_rLmbdSmkLimNr_CUR),
        so there the lambda maps are the limit. No P475 OEM file sets space=1,
        so on P475 that reading is marked ``unconfirmed`` -- a full-throttle
        log of FMTC_trqInrLimSmk settles it.
        """
        q = self.const("FlMng_swtqLimSmkQ_C")
        space = self.const("FlMng_swtSmokeSpace_C", 0)
        idx = int(round(self.curve_at("FlMng_dSmokeMapNr_CUR", 90.0, 0) or 0))
        if q is None:
            return "unknown", [], False
        if int(q) == 1 and int(space) == 0:
            names, mode = [SMOKE_Q_MAP], "quantity"
        else:
            names = ["FlMng_rLmbdSmkLim%d_MAP" % idx]
            mode = "lambda" if int(q) == 0 else "lambda-space"
            if mode == "lambda-space" and self.ent("FlMng_rLmbdSmkLimNr_CUR", ("curve",)) is None:
                mode = "lambda-space (unconfirmed on this software)"
        disabled = False
        for n in names:
            e = self.ent(n, ("map",))
            if e is not None and all(v == 0 for row in e.values() for v in row):
                disabled = True
        return mode, names, disabled

    def _max_torque_curve(self):
        """[(rpm, Nm)] -- the protection torque limit at sea-level pressure."""
        e = self.ent("EngPrt_trqLimP_MAP", ("map",))
        if e is None:
            return []
        ys = e.y_values()
        vals = e.values()
        n = grid.effective_len(ys)
        return [(ys[r], max(vals[r])) for r in range(n) if ys[r] > 0]

    def _static(self):
        r = self.res
        mode, names, placeholder = self.smoke_mode()
        self._smoke_names, self._smoke_mode, self._smoke_placeholder = names, mode, placeholder
        if placeholder:
            r.add(CRITICAL, "smoke", "Smoke limit disabled: its live table is all zeros",
                  "The switches (FlMng_swtqLimSmkQ_C / FlMng_swtSmokeSpace_C) make %s the "
                  "live smoke limit and every cell is 0, the usual way tuners switch the "
                  "smoke limit off. Nothing then caps fuel against the air in the cylinder "
                  "while the turbo spools or at altitude." % ", ".join(names))
        elif names and "unconfirmed" in mode:
            r.add(WARN, "smoke", "Which smoke table is live is not proven for this software",
                  "FlMng_swtSmokeSpace_C = 1 is the BHW/P350 arrangement, where the lambda maps "
                  "(%s) are the smoke limit. No stock file of this software family uses it, so "
                  "it is not proven here: it could equally select the all-zero quantity maps and "
                  "leave only the boost-pressure smoke limit FlMng_qPresSmoke_MAP. A full-throttle "
                  "'fuel' log settles it: FMTC_trqInrLimSmk following the air mass means the "
                  "lambda maps are live; pinned high means they are not." % ", ".join(names))
        elif names:
            r.add(INFO, "smoke", "Smoke limit: %s mode, live table %s" % (mode, ", ".join(names)),
                  "Decided from FlMng_swtqLimSmkQ_C, FlMng_swtSmokeSpace_C and "
                  "FlMng_dSmokeMapNr_CUR; the boost-pressure smoke limit "
                  "FlMng_qPresSmoke_MAP acts alongside it.")
        warn, crit, src = self.egt_limits()
        if src:
            r.add(INFO, "egt", "Exhaust-temperature derate starts at %.0f deg C" % warn,
                  "EngPrt_facTempPreTrbn_MAP first cuts torque at %.0f deg C and reaches its "
                  "0.90 row at %.0f deg C; those are the warning and critical levels used for "
                  "logged EGPpCD_tPreTrbn (pre-turbine sensor)." % (warn, crit))

        # Highest quantity the torque path can command vs the axes that must cover it.
        fmtc = self.ent("FMTC_trq2qBas_MAP", ("map",))
        tq = self._max_torque_curve()
        if fmtc is not None and tq:
            peak = []
            for rpm, nm in tq:
                if 1000 <= rpm <= 4800:
                    peak.append((rpm, nm, grid.lookup(fmtc, rpm, nm)))
            if peak:
                qmax = max(p[2] for p in peak)
                nm_max = max(p[1] for p in peak)
                self._q_reach = qmax
                axes = []
                for n in DURATION_MAPS[1:5]:
                    e = self.ent(n, ("map",))
                    if e is not None:
                        axes.append((n, grid.axis_max(e, "y")))
                soi = self.ent("InjCrv_phiBas9_GMAP", ("map",))
                if soi is not None:
                    axes.append(("InjCrv_phiBas9_GMAP", grid.axis_max(soi, "y")))
                short = [(n, a) for n, a in axes if a + 0.5 < qmax]
                detail = ("EngPrt_trqLimP_MAP allows up to %.0f Nm; FMTC_trq2qBas_MAP turns "
                          "that into up to %.1f mg/stroke." % (nm_max, qmax))
                if short:
                    r.add(WARN, "injection", "Commanded quantity can exceed the timing/duration axes",
                          detail + " These tables stop short of that and hold their last "
                          "row, so the extra quantity gets the top row's duration/timing: "
                          + ", ".join("%s (IQ axis ends at %.1f)" % s for s in short)
                          + ". Extend the axes (and fill the new rows) before raising torque.")
                else:
                    r.add(INFO, "injection", "Quantity axes cover the torque path", detail)

        # Boost limits.
        svbl = self.const("PCR_pBDesMax_C")
        limp = self.const("PCR_pLim_C")
        tmax = 0.0
        for n in TARGET_MAPS:
            e = self.ent(n, ("map",))
            if e is not None:
                tmax = max(tmax, max(max(row) for row in e.values()))
        if svbl is not None and tmax:
            if tmax > svbl:
                r.add(WARN, "boost", "Boost target map exceeds the single-value limit",
                      "Target maps reach %.0f hPa but PCR_pBDesMax_C clips at %.0f." % (tmax, svbl))
        fac = self.ent("PCR_facDesBas_CUR", ("curve",))
        if fac is not None:
            f_sea = grid.lookup(fac, 1013.0)
            f_alt = grid.lookup(fac, 700.0)
            r.add(INFO, "boost", "Boost target is blended from two maps by atmospheric pressure",
                  "PCR_facDesBas_CUR = %.2f at 1013 hPa and %.2f at 700 hPa. Bas2 holds "
                  "the lower targets and higher actuator duties of the two, the shape of "
                  "an altitude map, so Bas most likely governs at sea level; the logged "
                  "setpoint (boost preset) confirms which map your edits reach." % (f_sea, f_alt))

    # ------------------------------------------------------------ coverage
    def _coverage(self):
        rates = [r.rate() for r in self.runs if r.rate() > 0]
        rate = sorted(rates)[len(rates) // 2] if rates else 0.0
        for rec in RECIPES:
            missing = [x for x in rec.needs if not self._any(x)]
            self.res.coverage[rec.key] = missing
        rows = []
        for rec in RECIPES:
            miss = self.res.coverage[rec.key]
            rows.append([rec.title, "yes" if not miss else "no",
                         ", ".join(ROLE_LABELS.get(m, m) for m in miss) or "-"])
        dur = sum(r.duration() for r in self.runs)
        self.res.add(INFO, "logs", "%d log(s), %.0f s, ~%.1f samples/s"
                     % (len(self.runs), dur, rate),
                     "Which analyses the logs support:", (["Analysis", "Possible", "Missing"], rows))
        if 0 < rate < 4:
            self.res.add(WARN, "logs", "Low sample rate (%.1f/s)" % rate,
                         "Full-load pulls last a few seconds; fewer groups per log (the "
                         "presets pick the minimum) or a higher logging speed gives the "
                         "boost and fuelling analyses more points per pull.")

    # ------------------------------------------------------------ boost target
    def _boost_target(self):
        bas, bas2 = self.ent(TARGET_MAPS[0], ("map",)), self.ent(TARGET_MAPS[1], ("map",))
        fac = self.ent("PCR_facDesBas_CUR", ("curve",))
        if bas is None or not self._any("boost_spec"):
            return
        svbl = self.const("PCR_pBDesMax_C", 1e9)
        maxap = self.ent("PCR_pBDesMaxAP_MAP", ("map",))
        errs = defaultdict(list)
        n = 0
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq", "boost_spec")):
                spec = v["boost_spec"]
                atm = run.get("atm", i) or 1013.0
                if spec < atm + 50:
                    continue
                cap = svbl
                if maxap is not None:
                    cap = min(cap, grid.lookup(maxap, v["rpm"], atm))
                if spec >= cap - 15:
                    continue
                a = grid.lookup(bas, v["rpm"], v["iq"])
                b = grid.lookup(bas2, v["rpm"], v["iq"]) if bas2 is not None else a
                f = grid.lookup(fac, atm) if fac is not None else 1.0
                errs["Bas"].append(abs(spec - a))
                if bas2 is not None:
                    errs["Bas2"].append(abs(spec - b))
                    errs["blend"].append(abs(spec - (f * a + (1 - f) * b)))
                    errs["blend_rev"].append(abs(spec - ((1 - f) * a + f * b)))
                n += 1
        if n < 15:
            self.res.add(INFO, "boost", "Not enough un-clipped setpoint samples to identify the target map",
                         "%d usable rows (need 15 where the setpoint is below both limiters)." % n)
            return
        med = {k: sorted(v)[len(v) // 2] for k, v in errs.items()}
        best = min(med, key=med.get)
        self.res.boost_model = best if med[best] <= 60 else ""
        desc = {"Bas": "PCR_pBDesBas_MAP alone",
                "Bas2": "PCR_pBDesBas2_MAP alone",
                "blend": "facDesBas(atm) x Bas + (1 - facDesBas) x Bas2",
                "blend_rev": "(1 - facDesBas(atm)) x Bas + facDesBas x Bas2"}
        rows = [[desc[k], _fmt(v)] for k, v in sorted(med.items(), key=lambda kv: kv[1])]
        if not self.res.boost_model:
            self.res.add(WARN, "boost", "Logged boost setpoint does not match either target map",
                         "Best fit is still %.0f hPa off (median). Something else shapes the "
                         "setpoint (a correction map, a different quantity input), so the pilot "
                         "is not corrected from these logs." % med[best], (["Model", "Median error hPa"], rows))
            return
        # What that means for the two maps at the pressures actually logged.
        atms = [run.get("atm", i) for run in self.runs for i in range(len(run))
                if run.get("atm", i)] or [1013.0]
        atm_med = sorted(atms)[len(atms) // 2]
        f = grid.lookup(fac, atm_med) if fac is not None else 1.0
        wb = {"Bas": 1.0, "Bas2": 0.0, "blend": f, "blend_rev": 1 - f}[best]
        governs = ("PCR_pBDesBas_MAP" if wb > 0.9 else "PCR_pBDesBas2_MAP" if wb < 0.1
                   else "both maps (%.0f%% / %.0f%%)" % (wb * 100, (1 - wb) * 100))
        self.res.add(GOOD, "boost", "Boost setpoint identified: %s" % desc[best],
                     "Median error %.0f hPa over %d rows. At the logged atmospheric pressure "
                     "(~%.0f hPa) the setpoint comes from %s; edits to the other target map "
                     "only matter at other altitudes." % (med[best], n, atm_med, governs),
                     (["Model", "Median error hPa"], rows))

    def _pilot_weights(self, atm):
        """(weight of Bas pilot, weight of Bas2 pilot) under the identified model."""
        fac = self.ent("PCR_facCtlBas_CUR", ("curve",))
        f = grid.lookup(fac, atm) if fac is not None else 1.0
        m = self.res.boost_model
        if m == "Bas":
            return 1.0, 0.0
        if m == "Bas2":
            return 0.0, 1.0
        if m == "blend":
            return f, 1 - f
        if m == "blend_rev":
            return 1 - f, f
        return None

    # ------------------------------------------------------------ boost quality
    def _pulls(self, run):
        if not run.has("pedal"):
            return []
        return self._segments(run, lambda i: self._wot(run, i) and self._warm(run, i), 1.2)

    def _boost_quality(self):
        if not (self._any("boost") and self._any("boost_spec")):
            return
        devmax = self.curve_at("PCR_pBGDevMax_CUR", 90.0)
        rows, worst_over, worst_under = [], 0.0, 0.0
        self._overshoots = []
        for run in self.runs:
            for a, b in self._pulls(run):
                over, peak_i, reach_t, dev = -1e9, None, None, []
                for i in range(a, b + 1):
                    bo, sp = run.get("boost", i), run.get("boost_spec", i)
                    if bo is None or sp is None:
                        continue
                    if bo - sp > over:
                        over, peak_i = bo - sp, i
                    if reach_t is None and bo >= sp - 100:
                        reach_t = run.t[i] - run.t[a]
                    if run.t[i] - run.t[a] > 1.5:
                        dev.append(sp - bo)
                if peak_i is None:
                    continue
                r0, r1 = run.get("rpm", a), run.get("rpm", b)
                under = (sum(dev) / len(dev)) if dev else None
                rows.append([run.name, "%s-%s" % (_fmt(r0), _fmt(r1)), _fmt(over),
                             _fmt(reach_t, 1), _fmt(under)])
                worst_over = max(worst_over, over)
                if under is not None:
                    worst_under = max(worst_under, under)
                if over >= self.o.overshoot_trim_at:
                    self._overshoots.append((run, peak_i))
        if not rows:
            self.res.add(INFO, "boost", "No full-throttle pulls found",
                         "Needs accelerator pedal >= %.0f%% for >= 1.2 s with a warm engine."
                         % self.o.wot_pedal)
            return
        table = (["Log", "rpm", "Max overshoot hPa", "t to setpoint s", "Mean shortfall hPa"], rows)
        lim = devmax or 400.0
        if worst_over >= 0.6 * lim:
            sev, title = CRITICAL, "Boost overshoot close to the fault threshold"
        elif worst_over >= 150:
            sev, title = WARN, "Boost overshoot on full-throttle pulls"
        elif worst_under >= 150:
            sev, title = WARN, "Boost falls short of the setpoint under full load"
        else:
            sev, title = GOOD, "Boost tracks the setpoint on full-throttle pulls"
        self.res.add(sev, "boost", title,
                     "Worst overshoot %.0f hPa, worst sustained shortfall %.0f hPa; the ECU's "
                     "maximum control deviation (PCR_pBGDevMax_CUR, warm) is %.0f hPa."
                     % (worst_over, worst_under, lim), table)

    # ------------------------------------------------------------ pilot learning
    def _pilot(self):
        maps = [self.ent(n, ("map",)) for n in PILOT_MAPS]
        if maps[0] is None or not all(self._any(r) for r in ("boost", "boost_spec", "n75", "iq", "rpm")):
            return
        if not self.res.boost_model:
            self.res.add(INFO, "boost", "Pilot map not corrected",
                         "The logged setpoint could not be matched to the target maps, so it "
                         "is not known which pilot map the controller is using.")
            return
        # Which way the actuator works is read from the controller itself:
        # while boost is short of the setpoint the governor adds duty on top of
        # the pilot if more duty = more boost, and takes it away if not.
        more_duty_more_boost = self._duty_direction(maps)

        acc = [defaultdict(float), defaultdict(float)]     # weight
        off = [defaultdict(float), defaultdict(float)]     # weighted offset
        used = 0
        for run in self.runs:
            def steady(i, run=run):
                bo, sp, du = run.get("boost", i), run.get("boost_spec", i), run.get("n75", i)
                if None in (bo, sp, du, run.get("rpm", i), run.get("iq", i)):
                    return False
                atm = run.get("atm", i) or 1000.0
                return (self._warm(run, i) and sp >= atm + 150
                        and abs(bo - sp) <= self.o.steady_err and 5.0 < du < 95.0)
            for a, b in self._segments(run, steady, self.o.steady_time):
                for i in range(a, b + 1):
                    rpm, iq, du = run.get("rpm", i), run.get("iq", i), run.get("n75", i)
                    w2 = self._pilot_weights(run.get("atm", i) or 1013.0)
                    if any(m is None and wm > 0.05 for m, wm in zip(maps, w2)):
                        continue          # a blend partner is missing: prediction unknown
                    pred = sum(wm * grid.lookup(m, rpm, iq)
                               for m, wm in zip(maps, w2) if m is not None and wm > 0)
                    for k, (m, wm) in enumerate(zip(maps, w2)):
                        if m is None or wm < 0.05:
                            continue
                        for r, c, w in grid.weights(m, rpm, iq):
                            acc[k][(r, c)] += w * wm
                            off[k][(r, c)] += w * wm * (du - pred)
                    used += 1
        trims = [defaultdict(float), defaultdict(float)]
        if more_duty_more_boost is True:
            for run, pk in getattr(self, "_overshoots", []):
                for i in range(pk, -1, -1):
                    if run.t[pk] - run.t[i] > 1.0:
                        break
                    rpm, iq = run.get("rpm", i), run.get("iq", i)
                    if rpm is None or iq is None:
                        continue
                    w2 = self._pilot_weights(run.get("atm", i) or 1013.0)
                    for k, (m, wm) in enumerate(zip(maps, w2)):
                        if m is None or wm < 0.05:
                            continue
                        for r, c, w in grid.weights(m, rpm, iq):
                            trims[k][(r, c)] += w * wm
        changed = 0
        for k, m in enumerate(maps):
            if m is None:
                continue
            cells = set(k2 for k2, w in acc[k].items() if w >= self.o.pilot_min_weight)
            cells |= set(k2 for k2, w in trims[k].items() if w >= 1.0)
            xs, ys = m.x_values(), m.y_values()
            for (r, c) in sorted(cells):
                old = m.cell(r, c)
                delta, why = 0.0, []
                if acc[k].get((r, c), 0) >= self.o.pilot_min_weight:
                    mean_off = off[k][(r, c)] / acc[k][(r, c)]
                    delta = self.o.pilot_gain * mean_off
                    why.append("settled duty %+.1f%% vs pilot" % mean_off)
                if trims[k].get((r, c), 0) >= 1.0:
                    delta -= self.o.overshoot_trim
                    why.append("ahead of %.0f+ hPa overshoot" % self.o.overshoot_trim_at)
                delta = max(-self.o.pilot_max_step, min(self.o.pilot_max_step, delta))
                if abs(delta) < 0.25:
                    continue
                new = max(0.0, min(100.0, old + delta))
                self._set(m, r, c, new, "boost pilot: " + "; ".join(why))
                self.res.cells.append(CellNote(m.name, r, c, xs[c], ys[r], old, new,
                                               acc[k].get((r, c), 0.0), "; ".join(why)))
                changed += 1
        if used == 0:
            self.res.add(INFO, "boost", "No settled boost-control windows in the logs",
                         "Pilot learning needs stretches where actual boost stays within "
                         "%.0f hPa of the setpoint for %.1f s (steady part-throttle driving, "
                         "and the top of each full-throttle pull)." % (self.o.steady_err, self.o.steady_time))
        elif changed:
            self.res.add(INFO, "boost", "Boost pilot: %d cell(s) corrected" % changed,
                         "Moved %.0f%% of the way toward the duty the controller settled at "
                         "(max %.1f%% per cell per pass). Re-log and repeat until the changes "
                         "fall below ~1%%." % (self.o.pilot_gain * 100, self.o.pilot_max_step))
        else:
            self.res.add(GOOD, "boost", "Boost pilot already matches the settled duty",
                         "%d steady samples; no cell off by more than a fraction of a percent." % used)
        if getattr(self, "_overshoots", []) and more_duty_more_boost is not True:
            self.res.add(INFO, "boost", "Overshoot trims skipped",
                         "The logs do not show which way the actuator works (the governor "
                         "was not seen adding duty while boost was short of the setpoint), "
                         "so no pre-emptive duty was taken out ahead of the overshoot.")

    def _duty_direction(self, maps):
        """True if the governor adds duty when boost is short, False if it
        removes it, None when the logs do not say."""
        num = n = 0
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq", "boost", "boost_spec", "n75")):
                err = v["boost_spec"] - v["boost"]
                atm = run.get("atm", i) or 1000.0
                if abs(err) < 50 or v["boost_spec"] < atm + 150:
                    continue
                w2 = self._pilot_weights(run.get("atm", i) or 1013.0) or (1.0, 0.0)
                if any(m is None and wm > 0.05 for m, wm in zip(maps, w2)):
                    continue
                pred = sum(wm * grid.lookup(m, v["rpm"], v["iq"])
                           for m, wm in zip(maps, w2) if m is not None and wm > 0)
                gov = v["n75"] - pred
                if abs(gov) < 1.0:
                    continue
                num += 1 if (gov > 0) == (err > 0) else -1
                n += 1
        if n < 10:
            return None
        if num >= 0.6 * n:
            return True
        if num <= -0.6 * n:
            return False
        return None

    @staticmethod
    def _at_most(e, value):
        """The largest value the cell can store that does not exceed ``value``
        (a limit must stay a limit after quantisation)."""
        v = value
        for _ in range(3000):
            if e.clamp(v) <= value + 1e-9:
                return v
            v -= 0.001
        return value

    def _set(self, e, r, c, value, reason):
        """Plan a cell, mirroring it into the axis padding past the table's
        real edge so the ECU never blends toward a stale filler value."""
        self.res.plan.set(e, r, c, value, reason)
        if e.kind != "map":
            return
        nr = grid.effective_len(e.y_values()) if e.y is not None else e.rows
        nc = grid.effective_len(e.x_values()) if e.x is not None else e.cols
        rows = range(r, e.rows) if r == nr - 1 else (r,)
        cols = range(c, e.cols) if c == nc - 1 else (c,)
        for rr in rows:
            for cc in cols:
                if (rr, cc) != (r, c):
                    self.res.plan.set(e, rr, cc, value, reason + " (axis padding)")

    # ------------------------------------------------------------ fuelling
    def _lambda(self, run, i):
        m, q = run.get("maf", i), run.get("iq", i)
        if m is None or q is None or q < 3:
            return None
        return m / (q * STOICH)

    def _limiter(self, run, i):
        cand = []
        for role, name in (("trq_drv", "driver's wish"), ("trq_lim", "torque limit"),
                           ("trq_smk", "smoke limit"), ("trq_gbx", "gearbox")):
            v = run.get(role, i)
            if v is not None and v > 1.0:        # 0 = path inactive / not computed
                cand.append((v, name))
        if not cand:
            return None
        cand.sort()
        fin = run.get("trq_final", i)
        if fin is None:
            return cand[0][1]
        drv = run.get("trq_drv", i)
        if drv is not None and drv < fin - max(3.0, 0.03 * fin):
            return "driver's wish"          # asking for less than any limit allows
        # The limiter is the candidate the final limit actually equals; a
        # lower candidate the final value ignores is not in the path.
        for v, name in cand:
            if abs(v - fin) <= max(3.0, 0.03 * fin):
                return name
        return "other limit"

    def _fuel(self):
        if not all(self._any(r) for r in ("rpm", "iq", "pedal")):
            return
        bins = defaultdict(lambda: {"n": 0, "iq": [], "lam": [], "maf": [], "boost": [], "lim": Counter()})
        smoke_active = 0
        wot_rows = 0
        lim_rows = 0
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq")):
                if self._asks_more(run, i) or self._wot(run, i):
                    lim_rows += 1
                    if self._limiter(run, i) == "smoke limit":
                        smoke_active += 1
                if not self._wot(run, i):
                    continue
                wot_rows += 1
                b = int(v["rpm"] // 250) * 250
                d = bins[b]
                d["n"] += 1
                d["iq"].append(v["iq"])
                lam = self._lambda(run, i)
                if lam is not None:
                    d["lam"].append(lam)
                    d["maf"].append(run.get("maf", i))
                bo = run.get("boost", i)
                if bo is not None:
                    d["boost"].append(bo)
                lim = self._limiter(run, i)
                if lim:
                    d["lim"][lim] += 1
        self._wot_rows = wot_rows
        self._lim_rows = lim_rows
        self._smoke_active = smoke_active
        if not bins:
            return
        rows = []
        rich = []
        leanmin = 99.0
        qmax = 0.0
        for b in sorted(bins):
            d = bins[b]
            lmin = min(d["lam"]) if d["lam"] else None
            if lmin is not None:
                leanmin = min(leanmin, lmin)
                if lmin < self.o.lambda_target:
                    rich.append((b, lmin))
            qmax = max(qmax, max(d["iq"]))
            lim = d["lim"].most_common(1)[0][0] if d["lim"] else "-"
            rows.append(["%d-%d" % (b, b + 250), str(d["n"]), _fmt(sum(d["iq"]) / len(d["iq"]), 1),
                         _fmt(max(d["iq"]), 1),
                         _fmt(sum(d["maf"]) / len(d["maf"])) if d["maf"] else "-",
                         _fmt(lmin, 2), _fmt(sum(d["boost"]) / len(d["boost"])) if d["boost"] else "-",
                         lim])
        table = (["rpm", "n", "IQ mean", "IQ max", "Air mg", "lambda min", "Boost hPa", "Active limiter"], rows)
        self._q_logged_max = qmax
        if leanmin < self.o.lambda_floor:
            sev, title = CRITICAL, "Full-load lambda below the floor (%.2f)" % leanmin
        elif rich:
            sev, title = WARN, "Full-load lambda richer than target in %d rpm band(s)" % len(rich)
        elif leanmin < 90:
            sev, title = GOOD, "Full-load lambda at or leaner than target (min %.2f)" % leanmin
        else:
            sev, title = INFO, "Full-load fuelling (air mass not logged, lambda unknown)"
        self.res.add(sev, "fuel", title,
                     "lambda = air mass / (injected quantity x %.1f); target %.2f, floor %.2f. "
                     "Without an EGT sensor this is the main guard against smoke and "
                     "exhaust temperature." % (STOICH, self.o.lambda_target, self.o.lambda_floor), table)
        # Quantity beyond the duration axes.
        for n in DURATION_MAPS[1:5]:
            e = self.ent(n, ("map",))
            if e is not None and qmax > grid.axis_max(e, "y") + 0.5:
                self.res.add(CRITICAL if qmax > grid.axis_max(e, "y") + 5 else WARN, "injection", "Logged quantity exceeds the duration map axis",
                             "%.1f mg/stroke logged; %s ends at %.1f mg, so the duration for "
                             "the extra fuel is not in the calibration." % (qmax, n, grid.axis_max(e, "y")))
                break
        if self._any("trq_smk") and lim_rows:
            if smoke_active == 0:
                self.res.add(WARN if self._smoke_placeholder else INFO, "fuel",
                             "Smoke limit never capped fuel",
                             "Over %d limited rows (full throttle, or the driver asking for more "
                             "than the limit allows) the smoke-limit torque was never the lowest "
                             "limit%s." % (lim_rows, " -- consistent with the all-zero smoke table"
                                           if self._smoke_placeholder else ""))
            else:
                self.res.add(INFO, "fuel", "Smoke limit capped fuel in %.0f%% of limited rows"
                             % (100.0 * smoke_active / lim_rows),
                             "Limited rows = full throttle, or the driver asking for more torque "
                             "than the final limit allows (take-offs at part pedal count).")

    # ------------------------------------------------------------ smoke correction
    # ------------------------------------------------------------ which smoke table is live
    def _smoke_candidates(self):
        """[(label, entity, fn(run, i) -> mg)] -- every smoke quantity limit
        the calibration could be applying, evaluated at a logged row."""
        out = []
        pres = self.ent(PRES_SMOKE_MAP, ("map",))
        if pres is not None and self._any("boost"):
            out.append(("boost-pressure smoke limit", pres,
                        lambda run, i, e=pres: grid.lookup(e, run.get("rpm", i), run.get("boost", i))))
        qa = self.ent(SMOKE_Q_MAP, ("map",))
        if qa is not None and any(v for r in qa.values() for v in r):
            out.append(("air-mass smoke limit", qa,
                        lambda run, i, e=qa: grid.lookup(e, run.get("rpm", i), run.get("maf", i))))
        for n in getattr(self, "_smoke_names", []) or []:
            e = self.ent(n, ("map",))
            if e is not None and n.startswith("FlMng_rLmbdSmkLim") and any(v for r in e.values() for v in r):
                out.append(("lambda smoke map", e,
                            lambda run, i, e=e: run.get("maf", i) /
                            (STOICH * max(0.5, grid.lookup(e, run.get("rpm", i), run.get("maf", i))))))
        return out

    def _loaded_rows(self, need=("rpm", "maf", "boost", "iq")):
        """Warm rows under load: the driver asking for more than the limit, or
        pedal >= 40 %. Yields (run, i, v)."""
        for run in self.runs:
            for i, v in self._rows(run, need):
                ped = run.get("pedal", i)
                if v.get("iq", 99) < 10 or v.get("boost", 9999) <= 500:
                    continue
                if self._asks_more(run, i) or (ped is not None and ped >= 40.0):
                    yield run, i, v

    def _settling(self, run: Run, i: int) -> bool:
        """True while the air-mass reading still lags a pedal step.

        The air-flow meter answers a tip-in a few samples late, so the first
        rows read the old (low) air against the new (capped) fuel. On the
        2026-10-12 cold take-offs that read lambda 1.02-1.07 for ~0.2-0.4 s
        before settling at 1.15-1.2 -- "rich" only on paper. Passes 3/4 took
        such rows as proof and cut FlMng_qPresSmoke_MAP 2-3 mg below LQ stock
        in exactly the take-off cells. A row is settling when the pedal rose
        by ``tip_in_step`` or more within the last ``maf_settle_s``."""
        o = self.o
        if o.maf_settle_s <= 0 or not run.has("pedal"):
            return False
        ped = run.get("pedal", i)
        if ped is None:
            return False
        t = run.t[i]
        j = i - 1
        while j >= 0 and t - run.t[j] <= o.maf_settle_s + 1e-6:
            pj = run.get("pedal", j)
            if pj is not None and ped - pj >= o.tip_in_step:
                return True
            j -= 1
        return False

    def _cap_cluster(self, fn):
        """How the logged injected quantity sits against a candidate smoke
        table ``fn(run, i) -> mg`` on loaded rows.

        Rows the table capped all sit at the SAME distance from it -- the
        injected quantity is the capped quantity plus whatever the fuel path
        adds after the limit (on Bear's LQ a steady +1.6 mg). Rows held by
        something else (driver's wish, torque limit) scatter below. So the
        densest 1.6 mg window of (IQ - table) is the capped set and its median
        is the offset. Returns (offset, rows in window, loaded rows, keys)."""
        d = []
        for run, i, v in self._loaded_rows():
            try:
                d.append((v["iq"] - fn(run, i), (id(run), i)))
            except Exception:
                continue
        if not d:
            return 0.0, 0, 0, set()
        d.sort()
        best, lo = (0, 0, 0), 0
        for hi in range(len(d)):
            while d[hi][0] - d[lo][0] > 1.6:
                lo += 1
            if hi - lo + 1 > best[0]:
                best = (hi - lo + 1, lo, hi)
        n, lo, hi = best
        win = [x for x, _k in d[lo:hi + 1]]
        off = win[len(win) // 2]
        return off, n, len(d), {k for _x, k in d[lo:hi + 1]}

    def _identify_from_iq(self):
        """Without the torque channels (e.g. a boost-preset log), name the live
        smoke table from the injected quantity alone: the candidate that the
        most loaded rows sit a fixed offset above. Needs >= 10 such rows and a
        quarter of all loaded rows. Returns (entity, label, offset, n, total)."""
        best = None
        for label, e, fn in self._smoke_candidates():
            off, n, tot, _k = self._cap_cluster(fn)
            if n >= 10 and n >= 0.25 * tot and -1.0 <= off <= 4.0:
                if best is None or n > best[3]:
                    best = (e, label, off, n, tot)
        return best

    def _identify_smoke(self):
        """Name the smoke table the ECU actually applied, from the log: on rows
        where the smoke-limit torque IS the final limit, turn that torque back
        into mg (FMTC_trq2qBas_MAP) and see which candidate table gives the same
        number. Returns (entity, label, median error mg, rows) or None."""
        fmtc = self.ent("FMTC_trq2qBas_MAP", ("map",))
        if fmtc is None or not self._any("trq_smk") or not self._any("maf"):
            return None
        cands = self._smoke_candidates()
        if not cands:
            return None
        errs = {c[0]: [] for c in cands}
        n = 0
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "maf", "trq_smk", "trq_final")):
                lim = run.get("trq_lim", i)
                if not self._asks_more(run, i) or abs(v["trq_final"] - v["trq_smk"]) > max(5.0, 0.02 * v["trq_smk"]):
                    continue
                if lim is not None and lim <= v["trq_smk"] + 5:     # torque limit, not smoke
                    continue
                q = grid.lookup(fmtc, v["rpm"], v["trq_smk"])
                ok = True
                for label, e, fn in cands:
                    try:
                        errs[label].append(abs(fn(run, i) - q))
                    except Exception:
                        ok = False
                n += ok
        if n < 5:
            return None
        best = min(cands, key=lambda c: sorted(errs[c[0]])[len(errs[c[0]]) // 2])
        med = sorted(errs[best[0]])[len(errs[best[0]]) // 2]
        if med > self.o.smoke_id_tol:
            return None
        others = ", ".join("%s %.1f mg" % (c[0], sorted(errs[c[0]])[len(errs[c[0]]) // 2])
                           for c in cands if c is not best)
        return best[1], best[0], med, n, others

    def _smoke(self):
        if not self.o.correct_smoke or not self._any("maf") or not self._any("iq"):
            return
        ident = self._identify_smoke()
        if ident is not None:
            e, label, med, n, others = ident
            # the log has settled what the static check could only guess at
            self.res.findings = [f for f in self.res.findings
                                 if not (f.topic == "smoke" and "is not proven" in f.title)]
            self.res.add(INFO, "smoke", "Live smoke limit identified from the log: %s" % e.name,
                         "On %d rows where the smoke limit was the final limit, the logged "
                         "smoke-limit torque converts (FMTC_trq2qBas_MAP) to the %s's value within a "
                         "median %.1f mg. Other candidates: %s. This overrides what the switches "
                         "suggest." % (n, label, med, others or "none"))
            if e.name == PRES_SMOKE_MAP:
                self._correct_pres_map(e)
            elif e.name == SMOKE_Q_MAP:
                self._correct_q_map(e, from_model=False)
            else:
                self._correct_lambda_maps()
            return
        # only when the torque check was impossible, never to overrule it
        iq_ident = None if self._any("trq_smk") else self._identify_from_iq()
        if iq_ident is not None and iq_ident[0].name == PRES_SMOKE_MAP:
            e, label, off, n, tot = iq_ident
            self.res.findings = [f for f in self.res.findings
                                 if not (f.topic == "smoke" and "is not proven" in f.title)]
            self.res.add(INFO, "smoke", "Live smoke limit identified from injected quantity: %s" % e.name,
                         "%d of %d loaded rows sit %+.1f mg from the %s at their rpm/boost -- the "
                         "signature of a cap (rows held by anything else scatter below). No "
                         "smoke-torque channel was logged, so this stands in for the torque check."
                         % (n, tot, off, label))
            self._correct_pres_map(e)
            return
        if self._smoke_placeholder:
            self.res.add(INFO, "fuel", "Smoke limit not corrected",
                         "Its live table is all zeros (disabled); fill it with a lambda "
                         "target first, then let the logs refine it.")
            return
        if "unconfirmed" in self._smoke_mode and not getattr(self, "_smoke_active", 0):
            self.res.add(INFO, "fuel", "Smoke map not corrected",
                         "The logs don't show the smoke limit acting, and on this software "
                         "the lambda maps are not proven to be the live table.")
            return
        if self._smoke_mode == "quantity":
            self._correct_q_map(self.ent(SMOKE_Q_MAP, ("map",)), from_model=False)
        elif self._smoke_names:
            self._correct_lambda_maps()

    def _smoke_rows(self):
        """Full-load rows with the smoke maps each may correct (weight per map)."""
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq", "maf"), warm=False):
                if not self._limited(run, i) or v["iq"] < 5:
                    continue
                maps = self._maps_for_row(self._smoke_maps, run, i)
                if maps:
                    yield run, i, v, v["maf"] / (v["iq"] * STOICH), self._limiter(run, i), maps

    def _correct_pres_map(self, e):
        """Correct FlMng_qPresSmoke_MAP (quantity over rpm x boost pressure).

        The target of each cell is the fuel that gives lambda_target with the
        air the engine actually breathes at that rpm and boost. That air comes
        from the log: every warm row under load (driver asking for more than the
        limit, or pedal >= 40 %) contributes its air per hPa of boost, weighted
        onto the cells around it, and the cell's own boost turns that back into
        air. Cells only rise where the log covers them (weight >= smoke_min_weight),
        by at most smoke_max_step per pass, and never past lambda_floor. A cell
        only comes DOWN where rows it actually capped ran richer than target.
        Kept non-decreasing in boost above a raised cell, since more boost means
        more air."""
        if e is None or not self._any("boost"):
            return 0
        o = self.o
        xs, ys = e.x_values(), e.y_values()
        # What the engine injects when this table caps it: table + offset.
        # Measured, not assumed -- assuming zero overshot the 2026-10-02 pass
        # by the LQ's +1.6 mg and ran take-offs at lambda ~1.10.
        off, n_cap, _tot, cap_keys = self._cap_cluster(
            lambda run, i: grid.lookup(e, run.get("rpm", i), run.get("boost", i)))
        if n_cap < 10 or not (-1.0 <= off <= 4.0):
            off, cap_keys = 0.0, set()
        self._smoke_offset = off
        w_air, air_per_hpa = defaultdict(float), defaultdict(float)
        w_cap, rich = defaultdict(float), defaultdict(float)
        settling = 0
        for run, i, v in self._loaded_rows(("rpm", "maf", "boost")):
            if self._settling(run, i):
                settling += 1           # lagging air reading: neither air nor "rich" evidence
                continue
            capped = (id(run), i) in cap_keys or (
                self._asks_more(run, i) and self._limiter(run, i) == "smoke limit")
            for r, c, w in grid.weights(e, v["rpm"], v["boost"]):
                w_air[(r, c)] += w
                air_per_hpa[(r, c)] += w * v["maf"] / v["boost"]
                iq = run.get("iq", i)
                if capped and iq is not None and iq > 1.0:
                    w_cap[(r, c)] += w
                    rich[(r, c)] += w * (v["maf"] / (iq * STOICH))
        new, notes = {}, {}
        for (r, c), w in w_air.items():
            if w < o.smoke_min_weight:
                continue
            air = air_per_hpa[(r, c)] / w * ys[r]
            old = e.cell(r, c)
            target = air / (STOICH * o.lambda_target) - off
            ceiling = air / (STOICH * o.lambda_floor) - off
            if target > old:
                val = min(target, old * (1 + o.smoke_max_step), ceiling)
                why = "air %.0f mg at this rpm/boost (from %.1f logged samples) -> lambda %.2f%s" % (
                    air, w, o.lambda_target, (" (injects %+.1f mg over the table)" % off) if off else "")
            else:
                wc = w_cap.get((r, c), 0.0)
                if wc < 0.5 or rich[(r, c)] / wc >= o.lambda_target:
                    continue            # not proven to run rich here: leave headroom alone
                val = max(target, old * (1 - o.smoke_max_step))
                why = "capped rows ran lambda %.2f, richer than %.2f%s" % (
                    rich[(r, c)] / wc, o.lambda_target,
                    (" (injects %+.1f mg over the table)" % off) if off else "")
            if abs(val - old) < 0.1:
                continue
            new[(r, c)] = val
            notes[(r, c)] = (why, w)
        self._smoke_settling_rows = settling
        if not new:
            return 0
        nr = grid.effective_len(ys)
        for c in {cc for (_r, cc) in new}:
            prev = None
            for r in range(nr):
                cur = new.get((r, c), e.cell(r, c))
                if prev is not None and cur < prev - 1e-6 and (r, c) not in new:
                    new[(r, c)] = prev
                    notes[(r, c)] = ("kept non-decreasing in boost above a raised cell", 0.0)
                    cur = prev
                prev = cur
        up = 0
        for (r, c), val in sorted(new.items()):
            old = e.cell(r, c)
            up += val > old
            why, w = notes[(r, c)]
            self._set(e, r, c, val, "smoke: " + why)
            self.res.cells.append(CellNote(e.name, r, c, xs[c], ys[r], old, e.clamp(val), w, why))
        self.res.add(INFO, "fuel", "Boost-pressure smoke limit: %d cell(s) corrected (%d up, %d down)"
                     % (len(new), up, len(new) - up),
                     "Toward lambda %.2f with the air the log shows at each rpm/boost, at most %.0f%% "
                     "per pass, never richer than %.2f. Capped rows inject %+.1f mg over the table "
                     "(measured from the log), so each cell is set that much below the fuel it "
                     "should allow.%s" % (o.lambda_target, 100 * o.smoke_max_step, o.lambda_floor, off,
                                         (" %d row(s) within %.1f s of a pedal step were left out: the "
                                          "air-mass reading still lags there." % (settling, o.maf_settle_s))
                                         if settling else ""))
        return len(new)

    def _correct_q_map(self, e, from_model: bool):
        if e is None:
            return 0
        o = self.o
        xs, ys = e.x_values(), e.y_values()
        acc, tgt = defaultdict(float), defaultdict(float)
        lim_smoke = defaultdict(float)
        for run, i, v, lam, lim, maps in self._smoke_rows():
            wm = sum(w for m, w in maps if m.name == e.name)
            if wm <= 0:
                continue
            want = v["maf"] / (STOICH * o.lambda_target)
            for r, c, w in grid.weights(e, v["rpm"], v["maf"]):
                w *= wm
                acc[(r, c)] += w
                # Rich: this cell must come down to what the air supports.
                # Lean and smoke-limited: it may come up toward the same value.
                tgt[(r, c)] += w * want
                if lim == "smoke limit":
                    lim_smoke[(r, c)] += w
        changed = 0
        cells = set()
        if from_model:
            cells = {(r, c) for r in range(grid.effective_len(ys)) for c in range(grid.effective_len(xs))}
        cells |= {k for k, w in acc.items() if w >= o.smoke_min_weight}
        for (r, c) in sorted(cells):
            old = e.cell(r, c)
            air = ys[r]
            model = air / (STOICH * o.lambda_target)
            ceiling = air / (STOICH * o.lambda_floor)
            w = acc.get((r, c), 0.0)
            if w >= o.smoke_min_weight:
                logged = tgt[(r, c)] / w
                # A cell's own axis value is exact; the logged mean blends the
                # neighbours, so trust the model and use the log to decide
                # direction and whether the cell matters at all.
                want = model
                if want >= old and lim_smoke.get((r, c), 0) < o.smoke_min_weight * 0.5 and not from_model:
                    continue          # never smoke-limited here: leave headroom alone
                why = "logged lambda -> target %.2f (%.0f samples, logged want %.1f)" % (
                    o.lambda_target, w, logged)
            elif from_model:
                want, why = model, "air/(14.5 x %.2f)" % o.lambda_target
            else:
                continue
            if want > old and not from_model:
                want = min(want, old * (1 + o.smoke_max_step))
            want = min(want, ceiling)
            if abs(want - old) < 0.1:
                continue
            self._set(e, r, c, want, "smoke: " + why)
            self.res.cells.append(CellNote(e.name, r, c, xs[c], ys[r], old, e.clamp(want), w, why))
            changed += 1
        if changed and not from_model:
            self.res.add(INFO, "fuel", "Smoke map: %d cell(s) corrected toward lambda %.2f"
                         % (changed, o.lambda_target))
        return changed

    def _correct_lambda_maps(self):
        """Correct every lambda smoke map the logged rows were read from, each
        row weighted by how much the coolant selection uses that map."""
        o = self.o
        acc, ratio = defaultdict(float), defaultdict(float)
        kmin = defaultdict(float)        # richest factor that keeps every row >= floor
        ents = {}
        for run, i, v, lam, lim, maps in self._smoke_rows():
            if lim != "smoke limit" and lam >= o.lambda_target:
                continue
            for e, wm in maps:
                ents[e.name] = e
                for r, c, w in grid.weights(e, v["rpm"], v["maf"]):
                    key = (e.name, r, c)
                    acc[key] += w * wm
                    ratio[key] += w * wm * (o.lambda_target / lam)
                    kmin[key] = max(kmin[key], o.lambda_floor / lam)
        changed = 0
        for key, w in sorted(acc.items()):
            if w < o.smoke_min_weight:
                continue
            e = ents[key[0]]
            r, c = key[1], key[2]
            xs, ys = e.x_values(), e.y_values()
            k = ratio[key] / w
            k = max(1 - o.smoke_max_step, k, min(1.0, kmin[key]))   # leaning out is never capped
            old = e.cell(r, c)
            new = old * k
            if new < old:
                new = max(new, min(old, o.lambda_floor))           # never richer than the floor
            if abs(new - old) < 0.005:
                continue
            why = "lambda map x%.3f (logged lambda vs target %.2f)" % (k, o.lambda_target)
            self._set(e, r, c, new, "smoke: " + why)
            self.res.cells.append(CellNote(e.name, r, c, xs[c], ys[r], old, e.clamp(new), w, why))
            changed += 1
        if changed:
            self.res.add(INFO, "fuel", "Lambda smoke map(s) %s: %d cell(s) corrected"
                         % (", ".join(sorted(ents)), changed))

    # ------------------------------------------------------------ timing
    def egt_limits(self):
        """(warn, crit, from_calibration) for pre-turbine temperature.

        Read from the ECU's own EGT torque derate EngPrt_facTempPreTrbn_MAP
        (x = temperature, y = rpm): warn = lowest temperature where any rpm row
        drops below 1.0, crit = lowest where one reaches 0.90. Options override."""
        warn, crit = self.o.egt_warn, self.o.egt_crit
        e = self.ent("EngPrt_facTempPreTrbn_MAP", ("map",))
        cw = cc = None
        if e is not None:
            xs = e.x_values()
            nx = grid.effective_len(xs)
            for row in e.values():
                for c in range(nx):
                    if row[c] < 0.999 and (cw is None or xs[c] < cw):
                        cw = xs[c]
                    if row[c] <= 0.901 and (cc is None or xs[c] < cc):
                        cc = xs[c]
        src = cw is not None and cc is not None and warn is None and crit is None
        warn = warn if warn is not None else (round(cw, 1) if cw is not None else 800.0)
        crit = crit if crit is not None else (round(cc, 1) if cc is not None else 850.0)
        return warn, crit, src

    # ------------------------------------------------------------ coolant selection
    #: Coolant temperatures the selection curves are sampled at (deg C).
    _CLT_SWEEP = tuple(float(t) for t in range(-40, 121))

    @staticmethod
    def _blend(sel, name_fmt, ent):
        """[(entity, weight)] for a fractional map-number selector."""
        if sel is None:
            return []
        i0 = int(math.floor(sel + 1e-9))
        f = sel - i0
        out = []
        for i, w in ((i0, 1.0 - f), (i0 + 1, f)):
            e = ent(name_fmt % i, ("map",))
            if e is not None and w > 0.02:
                out.append((e, w))
        return out

    def _read_warm(self, selector, min_w=0.0):
        """Names of the maps ``selector(clt)`` reads (with at least ``min_w`` of
        the blend) anywhere from warm_clt up."""
        return {e.name for t in self._CLT_SWEEP if t >= self.o.warm_clt
                for e, w in selector(t) if w >= min_w}

    def _maps_for_row(self, selector, run, i, blend_ok=False):
        """The maps a logged row may correct: the ones the calibration reads at
        the row's coolant temperature -- and, for a warm-up row, only the ones a
        warm engine does not read, so warm-up data never moves the warm tune.
        ``blend_ok``: a map a warm engine only blends in as the minor part
        (under half, just above warm_clt) still counts as a warm-up map --
        right for timing, whose target (end of injection) is the same warm or
        cold; the smoke limit uses the strict rule."""
        clt = run.get("clt", i)
        maps = selector(clt if clt is not None else 90.0)
        if self._cold(run, i):
            if not self.o.tune_warmup:
                return []
            key = (getattr(selector, "__name__", str(selector)), blend_ok)
            warm = self._warm_cache.get(key)
            if warm is None:
                warm = self._warm_cache[key] = self._read_warm(selector, 0.5 if blend_ok else 0.0)
            maps = [(e, w) for e, w in maps if e.name not in warm]
        return maps

    def _map_clt_range(self, selector, e):
        """Coolant temperatures (sampled) where ``e`` dominates the selection,
        falling back to anywhere it is read."""
        dom = [t for t in self._CLT_SWEEP for m, w in selector(t) if m.name == e.name and w >= 0.5]
        return dom or [t for t in self._CLT_SWEEP for m, _w in selector(t) if m.name == e.name]

    def _smoke_maps(self, clt):
        """[(map entity, weight)] the smoke limit reads at this coolant temperature,
        by the same switches as :meth:`smoke_mode`."""
        q = self.const("FlMng_swtqLimSmkQ_C")
        space = self.const("FlMng_swtSmokeSpace_C", 0)
        if q is None:
            return []
        if int(q) == 1 and int(space) == 0:
            e = self.ent(SMOKE_Q_MAP, ("map",))
            return [(e, 1.0)] if e is not None else []
        sel = None
        if int(q) == 1:
            sel = self.curve_at("FlMng_rLmbdSmkLimNr_CUR", clt)
        if sel is None:
            sel = self.curve_at("FlMng_dSmokeMapNr_CUR", clt, 0)
        return [(e, w) for e, w in self._blend(sel, "FlMng_rLmbdSmkLim%d_MAP", self.ent)
                if any(v for row in e.values() for v in row)]      # an all-zero map is not a limit

    def _soi_maps(self, clt):
        """[(map entity, weight)] InjCrv_phiBas_CUR blends at this coolant temperature."""
        sel = self.curve_at("InjCrv_phiBas_CUR", clt if clt is not None else 90.0)
        return self._blend(sel, "InjCrv_phiBas%d_GMAP", self.ent)

    def _warm_soi_set(self):
        """Every SOI base map a warm engine (warm_clt .. 90 deg C) can read."""
        seen, out = set(), []
        t = self.o.warm_clt
        while t <= 90.0 + 1e-9:
            for e, _w in self._soi_maps(t):
                if e.name not in seen:
                    seen.add(e.name)
                    out.append(e)
            t += 2.5
        return out

    def _soi_cap(self, x, e=None):
        """Highest SOI worth writing at ``x`` rpm: the SOI limiter
        InjCrv_phiMIMax_MAP (less 0.3 deg) at the coolant temperatures where the
        map ``e`` is the one mostly read (warm_clt..90 deg C without a map),
        the tighter over that range, and never past soi_max / 30 deg."""
        lim = self.ent("InjCrv_phiMIMax_MAP", ("map",))
        cap = min(self.o.soi_max, SOI_HARD_MAX)
        if lim is None:
            return cap
        if e is None:
            temps = [self.o.warm_clt, 90.0]
        else:
            temps = [t for t in self._map_clt_range(self._soi_maps, e) if t <= 90.0] or [90.0]
            ys = lim.y_values()
            lo, hi = min(temps), max(temps)
            temps = [lo, hi] + [y for y in ys[:grid.effective_len(ys)] if lo < y < hi]
        return min([cap] + [grid.lookup(lim, x, t) - 0.3 for t in temps])

    def _egt_real(self):
        """True when the logged pre-turbine temperature behaves like a sensor.

        A missing or disabled sensor reads a substitute: the same value however
        hard the engine works. A real one spans hundreds of degrees between
        idle and a pull."""
        vals = [run.get("egt", i) for run in self.runs for i in range(len(run))
                if run.get("egt", i) is not None and self._warm(run, i)]
        wot = sum(1 for run in self.runs for i in range(len(run))
                  if run.get("egt", i) is not None and self._wot(run, i) and self._warm(run, i))
        self._egt_span = (min(vals), max(vals)) if vals else (0.0, 0.0)
        if len(vals) < 20 or wot < 10:
            return None                     # no full-load rows: nothing to compare against
        return (max(vals) - min(vals)) >= 60.0

    def _egt(self):
        if not self._any("egt"):
            return
        real = self._egt_real()
        self._egt_ok = bool(real)
        if real is None:
            return
        if not real:
            lo, hi = self._egt_span
            self.res.add(WARN, "egt", "Logged exhaust temperature is a fixed substitute value",
                         "EGPpCD_tPreTrbn stayed between %.0f and %.0f deg C from light load to full "
                         "throttle, so no working pre-turbine sensor is feeding it. Exhaust "
                         "temperature cannot be used; lambda is the only guard." % (lo, hi))
            return
        warn, crit, _src = self.egt_limits()
        self._egt_warn = warn
        bins = defaultdict(list)
        hot_crit = hot_warn = 0
        for run in self.runs:
            seg_w = self._segments(run, lambda i: (run.get("egt", i) or 0) >= warn, 2.0)
            seg_c = self._segments(run, lambda i: (run.get("egt", i) or 0) >= crit, 2.0)
            hot_warn += len(seg_w)
            hot_crit += len(seg_c)
            for i, v in self._rows(run, ("rpm", "egt")):
                if self._wot(run, i):
                    bins[int(v["rpm"] // 250) * 250].append(v["egt"])
        rows = [["%d-%d" % (b, b + 250), str(len(v)), _fmt(max(v))] for b, v in sorted(bins.items())]
        peak = max((max(v) for v in bins.values()), default=0)
        if hot_crit:
            sev, title = CRITICAL, "Exhaust temperature above %.0f deg C for 2 s or more" % crit
        elif hot_warn:
            sev, title = WARN, "Exhaust temperature above %.0f deg C for 2 s or more" % warn
        else:
            sev, title = GOOD, "Exhaust temperature peaked at %.0f deg C" % peak
        self.res.add(sev, "egt", title,
                     "Pre-turbine (EGPpCD_tPreTrbn). Levels from the ECU's own derate map "
                     "(EngPrt_facTempPreTrbn_MAP): the ECU starts cutting torque at %.0f deg C. The "
                     "fixes are more air (boost), less fuel, or an earlier end of injection." % warn, (["rpm", "n", "EGT max deg C"], rows) if rows else None)

    def _soi(self):
        """Advance the SOI base maps where the logs show a late end of injection
        (or, with a working sensor, a hot exhaust) under load. The correction is
        split between the maps InjCrv_phiBas_CUR blends at the logged coolant
        temperature. Opt-in, one step per pass, never past the SOI limiter."""
        if not all(self._any(r) for r in ("rpm", "iq", "soi", "dur")):
            return
        o = self.o
        egt_ok = getattr(self, "_egt_ok", False)
        warn = getattr(self, "_egt_warn", None) or self.egt_limits()[0]
        acc, want, ents = defaultdict(float), defaultdict(float), {}
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq", "soi", "dur"), warm=False):
                if v["iq"] < o.soi_min_iq:
                    continue
                late = (v["dur"] - v["soi"]) - o.eoi_target
                hot = egt_ok and (run.get("egt", i) or 0) >= warn
                need = max(late, 0.0) + (0.5 if hot else 0.0)
                for e, wm in self._maps_for_row(self._soi_maps, run, i, blend_ok=True):
                    ents[e.name] = e
                    for r, c, w in grid.weights(e, v["rpm"], v["iq"]):
                        acc[(e.name, r, c)] += w * wm
                        want[(e.name, r, c)] += w * wm * need
        n, touched = 0, set()
        for (name, r, c), w in sorted(acc.items()):
            if w < o.pilot_min_weight:
                continue
            adv = min(want[(name, r, c)] / w, o.soi_step)
            if adv < 0.1:
                continue
            e = ents[name]
            xs, ys = e.x_values(), e.y_values()
            old = e.cell(r, c)
            new = self._at_most(e, min(old + adv, max(old, self._soi_cap(xs[c], e))))
            if new - old < 0.05:
                continue
            why = "end of injection %.1f deg later than %.0f ATDC target%s" % (
                want[(name, r, c)] / w, o.eoi_target, "; hot exhaust" if egt_ok else "")
            self._set(e, r, c, new, "soi: " + why)
            self.res.cells.append(CellNote(e.name, r, c, xs[c], ys[r], old, e.clamp(new), w, why))
            touched.add(name)
            n += 1
        if n:
            self.res.add(WARN, "injection", "Start of injection advanced in %d cell(s) (opt-in)" % n,
                         "In %s, at most %.1f deg per cell per pass, capped at InjCrv_phiMIMax_MAP and "
                         "%.0f deg BTDC. Warm-up rows only move the maps InjCrv_phiBas_CUR selects "
                         "below %.0f deg C coolant. Earlier injection raises peak cylinder pressure: listen for "
                         "knock, log the timing preset after every pass, and stop once end of injection "
                         "is on target." % (", ".join(sorted(touched)), o.soi_step, o.soi_max, o.warm_clt))

    def _timing(self):
        if not all(self._any(r) for r in ("rpm", "iq", "soi", "dur")):
            return
        lim_map = self.ent("InjCrv_phiMIMax_MAP", ("map",))
        bins = defaultdict(list)
        clipped = total = 0
        for run in self.runs:
            for i, v in self._rows(run, ("rpm", "iq", "soi", "dur")):
                eoi = v["dur"] - v["soi"]
                bins[(int(v["rpm"] // 500) * 500, int(v["iq"] // 10) * 10)].append((eoi, v["soi"], v["dur"]))
                total += 1
                if lim_map is not None:
                    clt = run.get("clt", i) or 90.0
                    if v["soi"] >= grid.lookup(lim_map, v["rpm"], clt) - 0.3:
                        clipped += 1
        rows, late = [], []
        for (rb, qb), vals in sorted(bins.items()):
            e = max(x[0] for x in vals)
            if e > self.o.eoi_max and len(vals) >= 3:
                late.append((rb, qb, e))
            rows.append(["%d-%d" % (rb, rb + 500), "%d-%d" % (qb, qb + 10), str(len(vals)),
                         _fmt(max(x[1] for x in vals), 1), _fmt(max(x[2] for x in vals), 1), _fmt(e, 1)])
        table = (["rpm", "IQ mg", "n", "SOI max BTDC", "Duration max", "EOI max ATDC"], rows)
        if late:
            self.res.add(WARN, "injection", "Late end of injection in %d cell(s)" % len(late),
                         "End of injection = duration - start (crank degrees). Past ~%.0f deg ATDC "
                         "the fuel burns late: little torque, a lot of exhaust heat. Worst: %s. Not "
                         "corrected unless you opt in ('Advance start of injection'): advancing SOI "
                         "raises cylinder pressure, so it moves at most one step per pass, capped by "
                         "the SOI limiter."
                         % (self.o.eoi_max, ", ".join("%d rpm/%d mg: %.1f" % x for x in late[:4])), table)
        else:
            self.res.add(GOOD, "injection", "End of injection within %.0f deg ATDC everywhere logged"
                         % self.o.eoi_max, "", table)
        if total and clipped / total > 0.05:
            self.res.add(INFO, "injection", "Start of injection at its limiter in %.0f%% of rows"
                         % (100.0 * clipped / total),
                         "InjCrv_phiMIMax_MAP is capping the requested start of injection.")

    # ------------------------------------------------------------ warm-up
    def _warmup_bands(self):
        """Coolant bands below warm_clt, split where InjCrv_phiBas_CUR changes
        map -- the calibration's own warm-up stages."""
        cur = self.ent("InjCrv_phiBas_CUR", ("curve",))
        xs = cur.x_values() if cur is not None else []
        edges = sorted({round(x, 1) for x in xs[:grid.effective_len(xs)]
                        if self.o.warmup_min_clt < x < self.o.warm_clt})
        if not edges:
            edges = [20.0, 40.0, 55.0]
        lows = [None] + edges
        highs = edges + [self.o.warm_clt]
        return list(zip(lows, highs))

    def _warmup(self):
        """Report fuelling, timing and air per warm-up coolant band."""
        if not self._any("clt") or not self._any("rpm"):
            return
        o = self.o
        bands = self._warmup_bands()
        stats = [dict(n=0, wot=0, lam=[], iq=[], eoi=[], lim=Counter(), air=[], clip=0, soi=0)
                 for _ in bands]
        lim_map = self.ent("InjCrv_phiMIMax_MAP", ("map",))
        for run in self.runs:
            for i in range(len(run)):
                c, rpm = run.get("clt", i), run.get("rpm", i)
                if c is None or rpm is None or c >= o.warm_clt or c < o.warmup_min_clt:
                    continue
                k = next((j for j, (_lo, hi) in enumerate(bands) if c < hi), len(bands) - 1)
                d = stats[k]
                d["n"] += 1
                soi, dur, iq = run.get("soi", i), run.get("dur", i), run.get("iq", i)
                if soi is not None and dur is not None and iq is not None and iq >= o.soi_min_iq:
                    d["eoi"].append(dur - soi)
                if soi is not None and lim_map is not None:
                    d["soi"] += 1
                    if soi >= grid.lookup(lim_map, rpm, c) - 0.3:
                        d["clip"] += 1
                m, ms = run.get("maf", i), run.get("maf_spec", i)
                if m is not None and ms and 1000 <= rpm <= 3500 and not self._wot(run, i):
                    d["air"].append(m - ms)
                if self._wot(run, i):
                    d["wot"] += 1
                    if iq is not None:
                        d["iq"].append(iq)
                    lam = self._lambda(run, i)
                    if lam is not None:
                        d["lam"].append(lam)
                    lim = self._limiter(run, i)
                    if lim:
                        d["lim"][lim] += 1
        if not any(d["n"] for d in stats):
            return
        rows, rich, late = [], [], []
        for (lo, hi), d in zip(bands, stats):
            if not d["n"]:
                continue
            label = ("below %.0f" % hi) if lo is None else "%.0f-%.0f" % (lo, hi)
            mid = hi - 1.0 if lo is None else (lo + hi) / 2.0
            maps = ", ".join("%s %.0f%%" % (e.name.replace("InjCrv_phiBas", "").replace("_GMAP", ""), 100 * w)
                             for e, w in self._soi_maps(mid))
            lmin = min(d["lam"]) if d["lam"] else None
            emax = max(d["eoi"]) if len(d["eoi"]) >= 3 else None
            if lmin is not None and lmin < o.lambda_floor:
                rich.append((label, lmin))
            if emax is not None and emax > o.eoi_max:
                late.append((label, emax))
            rows.append([label, maps or "-", str(d["n"]), str(d["wot"]), _fmt(lmin, 2),
                         _fmt(max(d["iq"]), 1) if d["iq"] else "-", _fmt(emax, 1),
                         _fmt(100.0 * d["clip"] / d["soi"]) + "%" if d["soi"] else "-",
                         d["lim"].most_common(1)[0][0] if d["lim"] else "-",
                         _fmt(sum(d["air"]) / len(d["air"]), 0) if len(d["air"]) >= 10 else "-"])
        head = ["Coolant deg C", "SOI map (phiBas)", "Rows", "Full-load rows", "lambda min",
                "IQ max", "EOI max ATDC", "SOI at limiter", "Active limiter", "Air vs spec mg"]
        warm_soi = sorted(self._read_warm(self._soi_maps, 0.5))
        cold_soi = sorted({e.name for t in self._CLT_SWEEP if o.warmup_min_clt <= t < o.warm_clt
                           for e, _w in self._soi_maps(t)} - set(warm_soi))
        cold_smk = sorted({e.name for t in self._CLT_SWEEP if o.warmup_min_clt <= t < o.warm_clt
                           for e, _w in self._smoke_maps(t)} - self._read_warm(self._smoke_maps))
        detail = ("Split where InjCrv_phiBas_CUR changes timing map. A warm-up row only corrects "
                  "maps a warm engine (%.0f deg C and up) does not run on, so warm-up logs never move "
                  "the warm tune (a timing map a warm engine only blends in as the minor part just "
                  "above that counts as a warm-up map). Start of injection: %s. Smoke limit: %s."
                  % (o.warm_clt,
                     ("warm-up rows can correct " + ", ".join(cold_soi) + " (with 'Advance start of "
                      "injection')") if cold_soi else "no warm-up-only maps",
                     ("warm-up rows can correct " + ", ".join(cold_smk)) if cold_smk else
                     "the same map is read warm and cold, so it is tuned from warm rows only"))
        if rich and not cold_smk:
            detail += (" Cold full-load lambda below %.2f in %s: the candidate lever that leaves the warm "
                       "limit alone is the smoke path's coolant correction FlMng_facCorEngTemp_CUR. "
                       "It is 1.0 in every file here and its effect is not verified, so it is not "
                       "changed automatically; if you try it, lower it a few percent in that band "
                       "and log again." % (o.lambda_floor, ", ".join(l for l, _v in rich)))
        if late:
            detail += (" Late end of injection in %s." % ", ".join("%s (%.1f)" % x for x in late))
        sev = WARN if (rich or late) else INFO
        title = ("Warm-up: %s" % "; ".join(filter(None, [
            ("rich at full load in %d band(s)" % len(rich)) if rich else "",
            ("late injection in %d band(s)" % len(late)) if late else ""]))) if (rich or late) \
            else "Warm-up (coolant below %.0f deg C): %d rows" % (o.warm_clt, sum(d["n"] for d in stats))
        if not o.tune_warmup:
            detail += " Warm-up corrections are switched off; this is a report only."
        self.res.add(sev, "warmup", title, detail, (head, rows))

    # ------------------------------------------------------------ air / EGR
    def _air(self):
        if not all(self._any(r) for r in ("maf", "maf_spec", "rpm")):
            return
        part, full = [], []
        for run in self.runs:
            for i, v in self._rows(run, ("maf", "maf_spec", "rpm")):
                if not 1000 <= v["rpm"] <= 3500 or v["maf_spec"] <= 0:
                    continue
                dev = v["maf"] - v["maf_spec"]
                (full if self._wot(run, i) else part).append(dev)
        if len(part) >= 20:
            m = sum(part) / len(part)
            sev = WARN if abs(m) > 40 else GOOD
            self.res.add(sev, "air", "Part-load air mass vs specification: mean %+.0f mg/stroke" % m,
                         "The EGR loop closes on AirCtl_mDesVal. A persistent offset means the EGR "
                         "valve, the air-mass meter or a boost leak is off -- fix that before "
                         "changing fuelling, because every lambda above is computed from this sensor.")
        if len(full) >= 10:
            m = sum(full) / len(full)
            if m < -80:
                self.res.add(WARN, "air", "Full-load air mass %.0f mg/stroke below specification" % -m,
                             "Typical of an ageing air-mass meter or a boost leak; the lambda "
                             "figures above would then read richer than the engine really is.")

    # ------------------------------------------------------------ cruise
    def _cruise_bins(self, runs, use_ecu):
        out = defaultdict(lambda: [0.0, 0.0, 0])    # key -> [sum l/h, sum km/h, n]
        for run in runs:
            if not run.has("vss"):
                continue
            def steady(i, run=run):
                v = run.get("vss", i)
                if v is None or v < 30 or not self._warm(run, i):
                    return False
                j = max(0, i - 3)
                v0 = run.get("vss", j)
                dt = run.t[i] - run.t[j]
                return v0 is not None and (dt <= 0 or abs(v - v0) / dt <= 1.0)
            for a, b in self._segments(run, steady, 5.0):
                for i in range(a, b + 1):
                    v, rpm, iq = run.get("vss", i), run.get("rpm", i), run.get("iq", i)
                    lph = run.get("fuel_lph", i) if use_ecu else None
                    if lph is None and rpm is not None and iq is not None:
                        lph = (iq * 1e-6 * rpm * 30 * self.o.cylinders) / DIESEL_KG_PER_L
                    if lph is None or v is None:
                        continue
                    g = run.get("gear", i)
                    key = (int(round(v / 10.0)) * 10, int(g) if g is not None else None)
                    d = out[key]
                    d[0] += lph
                    d[1] += v
                    d[2] += 1
        return out

    def _cruise(self):
        # The ECU's own consumption figure only when every log has it, so a
        # before/after pair never compares two different measures.
        use_ecu = all(r.has("fuel_lph") for r in self.runs + self.base if r.has("vss"))
        self._cruise_src_ecu = use_ecu
        cur = self._cruise_bins(self.runs, use_ecu)
        cur = {k: d for k, d in cur.items() if d[2] >= 10}
        if not cur:
            return
        base = ({k: d for k, d in self._cruise_bins(self.base, use_ecu).items() if d[2] >= 10}
                if self.base else {})
        rows = []
        deltas = []
        for k in sorted(cur, key=lambda k: (k[0], k[1] or 0)):
            s, v, n = cur[k]
            l100 = (s / n) / (v / n) * 100
            row = ["%d" % k[0], "-" if k[1] is None else str(k[1]), str(n), _fmt(s / n, 2), _fmt(l100, 2)]
            if base:
                b = base.get(k) or base.get((k[0], None))
                if b:
                    bl = (b[0] / b[2]) / (b[1] / b[2]) * 100
                    d = (l100 - bl) / bl * 100
                    deltas.append(d)
                    row += [_fmt(bl, 2), "%+.1f%%" % d]
                else:
                    row += ["-", "-"]
            rows.append(row)
        head = ["km/h", "Gear", "n", "l/h", "l/100 km"] + (["Baseline l/100 km", "Change"] if base else [])
        if deltas:
            m = sum(deltas) / len(deltas)
            sev = GOOD if m < -0.5 else WARN if m > 0.5 else INFO
            title = "Cruise consumption %+.1f%% vs baseline (mean over %d speed bands)" % (m, len(deltas))
        else:
            sev, title = INFO, "Steady-cruise consumption"
        self.res.add(sev, "cruise", title,
                     "From %s at steady speed (>= 5 s, < 1 km/h/s). The ECU's own consumption is "
                     "computed from injected quantity, so compare before/after logs from the same "
                     "route, speed and weather rather than trusting absolute numbers."
                     % ("CoEng_dvolFlConsum" if use_ecu else "injected quantity x rpm"),
                     (head, rows))

    # ------------------------------------------------------------ plan checks
    def _soi_trial(self):
        """A fixed, bounded advance in the cruise cells -- not log-derived. It
        makes the B file of an A/B economy test; the cruise comparison decides."""
        deg = max(0.0, min(2.0, self.o.soi_cruise_trial))
        qmax = max(5.0, min(40.0, self.o.soi_trial_max_iq))
        maps = self._warm_soi_set()
        if not deg or not maps:
            return
        n = 0
        for e in maps:
            xs, ys = e.x_values(), e.y_values()
            for r in range(grid.effective_len(ys)):
                for c in range(grid.effective_len(xs)):
                    if 1250 <= xs[c] <= 2750 and 5 <= ys[r] <= qmax:
                        old = e.cell(r, c)
                        new = self._at_most(e, min(old + deg, max(old, self._soi_cap(xs[c], e))))
                        if new - old >= 0.05:
                            why = "economy trial +%.1f deg (compare cruise logs)" % deg
                            self._set(e, r, c, new, "soi: " + why)
                            self.res.cells.append(CellNote(e.name, r, c, xs[c], ys[r], old, e.clamp(new), 0.0, why))
                            n += 1
        if n:
            self.res.add(INFO, "injection", "Economy trial: start of injection +%.1f deg in %d cruise cell(s)"
                         % (deg, n),
                         "1250-2750 rpm, 5-%.0f mg in %s (the maps a %.0f-90 deg C engine uses). Log the "
                         "cruise preset on the same route before and after, and keep it only if "
                         "consumption drops without knock or harshness."
                         % (qmax, ", ".join(e.name for e in maps), self.o.warm_clt))

    def _checks(self):
        o = self.o

        def pilot_step(plan):
            out = []
            for w in plan.effective_writes:
                if w.target.name in PILOT_MAPS and abs(w.new - w.old) > o.pilot_max_step + 0.02:
                    out.append(Problem(ERROR, "pilot-step", "pilot moves %.2f%% (max %.1f)"
                                       % (w.new - w.old, o.pilot_max_step),
                                       "%s[%d,%d]" % (w.target.name, w.row, w.col)))
            return out

        def smoke_floor(plan):
            out = []
            for w in plan.effective_writes:
                if w.target.name != SMOKE_Q_MAP:
                    continue
                air = w.target.y_values()[w.row]
                ceiling = air / (STOICH * o.lambda_floor)
                if w.new > ceiling + 0.05:
                    out.append(Problem(ERROR, "smoke-floor", "%.1f mg on %.0f mg air is richer than "
                                       "lambda %.2f" % (w.new, air, o.lambda_floor),
                                       "%s[%d,%d]" % (w.target.name, w.row, w.col)))
            return out

        def lambda_floor(plan):
            out = []
            for w in plan.effective_writes:
                if w.target.name.startswith("FlMng_rLmbdSmkLim") and w.new < w.old \
                        and w.new < o.lambda_floor - 1e-3:
                    out.append(Problem(ERROR, "lambda-floor", "lambda limit lowered to %.3f (floor %.2f)"
                                       % (w.new, o.lambda_floor),
                                       "%s[%d,%d]" % (w.target.name, w.row, w.col)))
            return out

        def smoke_monotonic(plan):
            out = []
            ep = next((t for t in plan.targets() if t.name == PRES_SMOKE_MAP), None)
            if ep is not None:
                nrow = grid.effective_len(ep.y_values())
                for c in range(grid.effective_len(ep.x_values())):
                    col = [plan.value_at(ep, r, c) for r in range(nrow)]
                    if any(b < a - 0.05 for a, b in zip(col, col[1:])):
                        out.append(Problem(WARNING, "smoke-shape",
                                           "more boost allows less fuel in rpm column %d" % c, ep.name))
            e = next((t for t in plan.targets() if t.name == SMOKE_Q_MAP), None)
            if e is None:
                return out
            nrow = grid.effective_len(e.y_values())
            for c in range(grid.effective_len(e.x_values())):
                col = [plan.value_at(e, r, c) for r in range(nrow)]
                if any(b < a - 0.05 for a, b in zip(col, col[1:])):
                    out.append(Problem(WARNING, "smoke-shape", "more air allows less fuel in column %d" % c,
                                       e.name))
            return out

        def soi_bounds(plan):
            out = []
            lim = self.ent("InjCrv_phiMIMax_MAP", ("map",))
            for w in plan.effective_writes:
                if not w.target.name.startswith("InjCrv_phiBas"):
                    continue
                step = max(o.soi_step, min(2.0, o.soi_cruise_trial))
                where = "%s[%d,%d]" % (w.target.name, w.row, w.col)
                if w.new - w.old > step + 0.02 or w.new < w.old - 0.02:
                    out.append(Problem(ERROR, "soi-step", "SOI moves %+.2f deg (max +%.1f)"
                                       % (w.new - w.old, step), where))
                x = w.target.x_values()[w.col]
                cap = self._soi_cap(x, w.target) + 0.3
                if w.new > max(cap, w.old) + 0.02:
                    out.append(Problem(ERROR, "soi-limit", "SOI %.2f deg is past the limit %.2f"
                                       % (w.new, cap), where))
            return out

        return [pilot_step, smoke_floor, lambda_floor, smoke_monotonic, soi_bounds]


def analyse(session, runs, options=None, baseline=()) -> Result:
    return LogTuner(session, runs, options, baseline).analyse()

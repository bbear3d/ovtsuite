# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Log-driven EDC16 tuning (ovtsuite.logtune).

The simulated car in ``logtune_sim`` reads the calibration the way the ECU
does and has known answers built in: the turbo needs +5 % duty over the pilot
between 1900 and 3100 rpm, and full-load lambda is rich at low rpm. One LQ
session is shared by the module (~2 s); everything else is pure data.
"""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import pytest  # noqa: E402

from ovtsuite.logtune import grid  # noqa: E402
from ovtsuite.logtune.samples import _pressure_factor  # noqa: E402

LQ = os.path.join(HERE, "..", "Reference", "Binaries", "03G906016LQ.bin")
needs_ref = pytest.mark.skipif(not os.path.exists(LQ), reason="reference files not present")


# ------------------------------------------------------------------ pure
class _Ax:
    pass


class _Ent:
    """Just enough of an Entity for the grid helpers."""

    def __init__(self, xs, ys, vals, kind="map"):
        self.kind, self._xs, self._ys, self._v = kind, xs, ys, vals
        self.rows, self.cols = len(vals), len(vals[0])
        self.x, self.y = _Ax(), (_Ax() if ys else None)

    def x_values(self):
        return self._xs

    def y_values(self):
        return self._ys

    def values(self):
        return self._v


def test_axis_padding_is_not_resolution():
    ax = [1500, 2500, 4700, 4701, 4702, 4703]
    assert grid.effective_len(ax) == 3
    assert grid.position(ax, 9000) == (2, 2, 0.0)
    # small-valued axes keep their real steps
    assert grid.effective_len([0.95, 1.0, 1.05, 1.1]) == 4


def test_bilinear_lookup_and_weights_sum_to_one():
    e = _Ent([1000, 2000], [10, 20], [[1.0, 2.0], [3.0, 4.0]])
    assert grid.lookup(e, 1500, 15) == pytest.approx(2.5)
    assert grid.lookup(e, 500, 5) == pytest.approx(1.0)        # clamped
    ws = grid.weights(e, 1250, 12)
    assert sum(w for _r, _c, w in ws) == pytest.approx(1.0)


@pytest.mark.parametrize("unit,vals,k", [
    ("kPa", [210.0], 10.0), ("bar", [2.1], 1000.0), ("mbar", [2100.0], 1.0),
    ("hPa", [2100.0], 1.0), ("", [2.1], 1000.0), ("", [210.0], 10.0), ("", [2100.0], 1.0),
])
def test_pressure_units(unit, vals, k):
    assert _pressure_factor(unit, vals) == k


# ------------------------------------------------------------------ with the LQ calibration
@pytest.fixture(scope="module")
def lq():
    if not os.path.exists(LQ):
        pytest.skip("reference files not present")
    from ovtsuite.session import TunerSession
    from ovtsuite.datalog.channels import ChannelNamer
    s = TunerSession(LQ)
    return s, ChannelNamer(s)


def _set_const(s, name, value):
    e = next(x for x in s.entities if x.name == name)
    s.edit_cells(e, [(0, 0, value)])


@pytest.fixture()
def lambda_space(lq):
    """The BHW/038997016N/T arrangement on the LQ: quantity limit from the lambda map space."""
    s, _n = lq
    _set_const(s, "FlMng_swtSmokeSpace_C", 1)
    yield
    s.undo()


def _logs(tmp_path, lq, pull_overshoot=(200, 60, 60)):
    import logtune_sim as sim
    s, n = lq
    car = sim.Car(s)
    rows = []
    for k, ov in enumerate(pull_overshoot):
        rows += sim.wot_pull(car, t0=k * 20.0, overshoot=ov)
    p = [sim.write_log(str(tmp_path / "boost.csv"), s, n, [1, 10, 11], rows),
         sim.write_log(str(tmp_path / "fuel.csv"), s, n, [1, 8, 9, 10], rows)]
    return p, car


@needs_ref
def test_recipes_resolve_to_this_calibrations_groups(lq):
    from ovtsuite.logtune import RECIPES, recipe_groups
    _s, n = lq
    for rec in RECIPES:
        groups, missing = recipe_groups(n, rec)
        assert groups and not missing, rec.key
    assert recipe_groups(n, RECIPES[0])[0] == [1, 10, 11]


@needs_ref
def test_columns_resolve_through_the_binary_without_labels(lq, tmp_path):
    """A VCDS-style log (no names) is identified from group/field alone."""
    from ovtsuite.datalog import logfile as lf
    from ovtsuite.logtune import run_from_log
    paths, _car = _logs(tmp_path, lq)
    log = lf.load(paths[0])
    for c in log.channels:
        c.label, c.signal = "Group %d / Cell %d" % (c.group, c.field + 1), ""
    run = run_from_log(log, lq[1])
    for role in ("rpm", "iq", "boost", "boost_spec", "n75", "atm", "pedal"):
        assert run.has(role), role


@needs_ref
def test_smoke_space_on_p475_is_marked_unconfirmed(lq, lambda_space):
    from ovtsuite.logtune.analysis import LogTuner
    mode, names, disabled = LogTuner(lq[0], []).smoke_mode()
    assert mode.startswith("lambda-space") and "unconfirmed" in mode
    assert names == ["FlMng_rLmbdSmkLim0_MAP"] and not disabled


BHW = os.path.join(HERE, "..", "Reference", "Tunes", "Stock.bin")


@pytest.mark.skipif(not os.path.exists(BHW), reason="BHW stock file not present")
def test_smoke_space_is_the_lambda_maps_on_bhw_stock():
    """P350 OEM: Q=1, space=1, quantity tables all zero, lambda maps calibrated."""
    from ovtsuite.session import TunerSession
    from ovtsuite.logtune.analysis import LogTuner
    assert LogTuner(TunerSession(BHW), []).smoke_mode() == (
        "lambda-space", ["FlMng_rLmbdSmkLim0_MAP"], False)


@needs_ref
def test_smoke_mode_quantity_map_on_stock_lq(lq):
    from ovtsuite.logtune.analysis import SMOKE_Q_MAP, LogTuner
    assert LogTuner(lq[0], []).smoke_mode() == ("quantity", [SMOKE_Q_MAP], False)


@needs_ref
def test_static_flags_all_zero_smoke_table(lq, lambda_space):
    from ovtsuite.logtune import CRITICAL, analyse
    s, _n = lq
    e = next(x for x in s.entities if x.name == "FlMng_rLmbdSmkLim0_MAP")
    s.edit_cells(e, [(r, c, 0.0) for r in range(e.rows) for c in range(e.cols)])
    try:
        res = analyse(s, [])
        assert any(f.severity == CRITICAL and f.topic == "smoke" for f in res.findings)
        assert not res.plan.effective_writes
    finally:
        s.undo()


@needs_ref
def test_boost_target_and_pilot_learning(lq, tmp_path):
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    import logtune_sim as sim
    s, n = lq
    paths, car = _logs(tmp_path, lq)
    res = analyse(s, runs_from_paths(paths, n), Options(correct_smoke=False))
    assert res.boost_model in ("Bas2", "blend")          # f(1010 hPa) = 0 -> Bas2
    pilot = [w for w in res.plan.effective_writes if w.target.name.startswith("PCR_rBPCtl")]
    assert pilot and all(w.target.name == "PCR_rBPCtlBas2_MAP" for w in pilot)
    xs = pilot[0].target.x_values()
    o = Options()
    for w in pilot:
        assert abs(w.new - w.old) <= o.pilot_max_step + 1e-6
    # inside the band that needs +5 %, un-trimmed cells go up
    up = [w for w in pilot if 2200 <= xs[w.col] <= 3000 and "overshoot" not in w.reason]
    assert up and all(w.new > w.old for w in up)
    # the 200 hPa overshoot near 2100 rpm earns a trim somewhere below it
    assert any("overshoot" in w.reason and w.new < w.old for w in pilot)
    assert res.plan.ok


@needs_ref
def test_lambda_space_corrected_only_when_logs_show_smoke_limiting(lq, tmp_path, lambda_space):
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    o = Options(correct_pilot=False)
    paths, _car = _logs(tmp_path, lq)                  # smoke torque never the limit
    res = analyse(s, runs_from_paths(paths, n), o)
    assert not res.plan.effective_writes               # unconfirmed + not acting: hands off
    rows = sim.wot_pull(sim.Car(s))
    for _t, v in rows:
        v["trq_smk"] = v["trq_final"] = v["trq_lim"] - 10
    p = sim.write_log(str(tmp_path / "smk.csv"), s, n, [1, 8, 9, 10], rows)
    res = analyse(s, runs_from_paths([p], n), o)
    names = {w.target.name for w in res.plan.effective_writes}
    assert names == {"FlMng_rLmbdSmkLim0_MAP"}
    for w in res.plan.effective_writes:
        assert w.new >= min(w.old, o.lambda_floor) - 1e-3
    assert res.plan.ok


@needs_ref
def test_plan_applies_as_one_undo_step(lq, tmp_path):
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    paths, _car = _logs(tmp_path, lq)
    res = analyse(s, runs_from_paths(paths, n), Options(correct_smoke=False))
    w = res.plan.effective_writes[0]
    before = w.target.cell(w.row, w.col)
    assert res.plan.apply() == len(res.plan.effective_writes)
    assert w.target.cell(w.row, w.col) == pytest.approx(w.new)
    s.undo()
    assert w.target.cell(w.row, w.col) == pytest.approx(before)


@needs_ref
def test_limiter_and_lambda_table(lq, tmp_path):
    from ovtsuite.logtune import analyse, runs_from_paths
    s, n = lq
    paths, _car = _logs(tmp_path, lq)
    res = analyse(s, runs_from_paths(paths[1:], n))
    fuel = next(f for f in res.findings if f.topic == "fuel" and f.table)
    assert {r[-1] for r in fuel.table[1]} == {"torque limit"}
    assert any(f.title.startswith("Smoke limit never capped") for f in res.findings)


@needs_ref
def test_cruise_before_after(lq, tmp_path):
    import logtune_sim as sim
    from ovtsuite.logtune import analyse, runs_from_paths
    s, n = lq
    car = sim.Car(s)
    a = sim.write_log(str(tmp_path / "before.csv"), s, n, [3, 15, 194, 213], sim.cruise(car))
    b = sim.write_log(str(tmp_path / "after.csv"), s, n, [3, 15, 194, 213],
                      sim.cruise(car, iq_scale=0.95))
    res = analyse(s, runs_from_paths([b], n), baseline=runs_from_paths([a], n))
    f = next(f for f in res.findings if f.topic == "cruise")
    assert "-5.0%" in f.title
    l100 = float(f.table[1][0][4])
    assert 3.0 < l100 < 12.0          # plausible l/100 km, not a unit slip


@needs_ref
def test_lambda_mode_never_plans_richer_than_floor(lq, tmp_path):
    """Lambda-mode smoke maps: a smoke-limited, lean log opens cells up, but
    no cell goes below the floor however lean the log was."""
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    sw = next(x for x in s.entities if x.name == "FlMng_swtqLimSmkQ_C")
    s.edit_cells(sw, [(0, 0, 0)])                       # lambda maps live
    try:
        car = sim.Car(s)
        rows = sim.wot_pull(car)
        for _t, v in rows:
            v["trq_smk"] = v["trq_final"] = v["trq_lim"] - 10
            v["maf"] = v["iq"] * 14.5 * 1.60            # very lean: invites richening
        p = sim.write_log(str(tmp_path / "lam.csv"), s, n, [1, 8, 9, 10], rows)
        o = Options(correct_pilot=False, lambda_floor=1.12)
        res = analyse(s, runs_from_paths([p], n), o)
        lam = [w for w in res.plan.effective_writes if w.target.name.startswith("FlMng_rLmbdSmkLim")]
        assert lam
        for w in lam:
            assert w.new >= min(w.old, o.lambda_floor) - 1e-3
            assert w.new >= w.old * (1 - o.smoke_max_step) - 1e-3
        assert res.plan.ok
    finally:
        s.undo()


def test_driver_request_below_the_limit_is_not_a_limiter():
    from ovtsuite.logtune.analysis import LogTuner
    from ovtsuite.logtune.samples import Run

    class _S:
        entities = []
    run = Run("x", t=[0.0], data={"trq_drv": [250.0], "trq_smk": [300.0],
                                  "trq_final": [300.0], "trq_lim": [345.0]})
    tuner = LogTuner(_S(), [run])
    assert tuner._limiter(run, 0) == "driver's wish"
    run.data["trq_drv"] = [460.0]
    assert tuner._limiter(run, 0) == "smoke limit"


def _timing_log(tmp_path, lq, model_soi=False, **kw):
    """``model_soi``: log the start the ECU's base set gives at the pull's
    coolant (the End of injection model), not phiBas9 -- what a real log of
    a cold engine shows."""
    import logtune_sim as sim
    s, n = lq
    car = sim.Car(s)
    rows = []
    for k in range(3):
        rows += sim.wot_pull(car, t0=k * 20.0, **kw)
    if model_soi:
        from ovtsuite import eoi
        calc = eoi.Calculator(eoi.models(s)[0])
        clt, late = kw.get("clt", 90.0), kw.get("eoi", 6.0)
        for _t, v in rows:
            v["soi"] = calc.cell(clt, v["rpm"], v["iq"]).soi_btdc
            v["dur"] = v["soi"] + late
    return sim.write_log(str(tmp_path / "t.csv"), s, n, [1, 4, 7, 10, 74], rows)


@needs_ref
def test_soi_advance_is_opt_in_and_bounded(lq, tmp_path):
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    from ovtsuite.logtune import grid
    s, n = lq
    runs = runs_from_paths([_timing_log(tmp_path, lq, eoi=13.0)], n)
    off = analyse(s, runs, Options(correct_pilot=False, correct_smoke=False))
    assert not [w for w in off.plan.effective_writes if w.target.name.startswith("InjCrv")]
    o = Options(correct_pilot=False, correct_smoke=False, correct_soi=True)
    res = analyse(s, runs, o)
    soi = [w for w in res.plan.effective_writes if w.target.name == "InjCrv_phiBas9_GMAP"]
    assert soi and res.plan.ok
    lim = next(x for x in s.entities if x.name == "InjCrv_phiMIMax_MAP")
    for w in soi:
        assert 0 < w.new - w.old <= o.soi_step + 1e-6
        assert w.target.y_values()[w.row] >= o.soi_min_iq - 10     # load cells only
        cap = min(o.soi_max, grid.lookup(lim, w.target.x_values()[w.col], 90.0))
        assert w.new <= max(cap, w.old) + 1e-6


@needs_ref
def test_soi_economy_trial_without_logs(lq):
    from ovtsuite.logtune import Options, analyse
    s, _n = lq
    res = analyse(s, [], Options(soi_cruise_trial=1.0))
    ws = res.plan.effective_writes
    assert ws and res.plan.ok
    for w in ws:
        x, y = w.target.x_values()[w.col], w.target.y_values()[w.row]
        assert 1250 <= x <= 2750 and 5 <= y <= 25
        assert 0 < w.new - w.old <= 1.0 + 1e-6


@needs_ref
def test_soi_economy_trial_iq_ceiling_is_an_option(lq):
    from ovtsuite.logtune import Options, analyse
    s, _n = lq
    ws = analyse(s, [], Options(soi_cruise_trial=1.0, soi_trial_max_iq=35.0)).plan.effective_writes
    ys = [w.target.y_values()[w.row] for w in ws]
    assert ws and max(ys) > 25 and max(ys) <= 35


@needs_ref
def test_egt_substitute_value_is_recognised(lq, tmp_path):
    from ovtsuite.logtune import analyse, runs_from_paths
    s, n = lq
    res = analyse(s, runs_from_paths([_timing_log(tmp_path, lq, egt=450.0)], n))
    assert any(f.topic == "egt" and "substitute" in f.title for f in res.findings)


@needs_ref
def test_real_egt_hot_is_flagged(lq, tmp_path):
    from ovtsuite.logtune import CRITICAL, analyse, runs_from_paths
    s, n = lq
    res = analyse(s, runs_from_paths([_timing_log(tmp_path, lq, egt=lambda rpm: 350 + rpm * 0.16)], n))
    f = next(f for f in res.findings if f.topic == "egt" and f.table)
    assert f.severity == CRITICAL                      # >850 deg C from ~3100 rpm to the top of each pull


@needs_ref
def test_egt_limits_come_from_the_derate_map(lq):
    from ovtsuite.logtune.analysis import LogTuner
    warn, crit, src = LogTuner(lq[0], []).egt_limits()
    assert src and (round(warn), round(crit)) == (775, 825)   # LQ EngPrt_facTempPreTrbn_MAP


@needs_ref
def test_soi_correction_follows_the_coolant_map_blend(lq, tmp_path):
    """At 70 deg C a calibration that blends two SOI maps gets both corrected."""
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    cur = next(x for x in s.entities if x.name == "InjCrv_phiBas_CUR")
    xs = cur.x_values()
    c75 = min(range(len(xs)), key=lambda c: abs(xs[c] - 75.0))
    s.edit_cells(cur, [(0, c75, 8.0)])                   # 61 C -> 9, 75 C -> 8: 70 C blends 8/9
    try:
        runs = runs_from_paths([_timing_log(tmp_path, lq, eoi=13.0, clt=70.0)], n)
        res = analyse(s, runs, Options(correct_pilot=False, correct_smoke=False, correct_soi=True))
        maps = {w.target.name for w in res.plan.effective_writes}
        assert {"InjCrv_phiBas8_GMAP", "InjCrv_phiBas9_GMAP"} <= maps
        assert res.plan.ok
    finally:
        s.undo()


def test_warm_threshold_is_70():
    from ovtsuite.logtune import Options
    assert Options().warm_clt == 70.0


# ------------------------------------------------------------------ warm-up
@needs_ref
def test_warmup_rows_correct_only_warmup_timing_maps(lq, tmp_path):
    """At 40 deg C the LQ reads phiBas7/8; a warm engine only ever reads 9.
    Warm-up rows correct 7/8 and never 9; switched off, they correct nothing."""
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    runs = runs_from_paths([_timing_log(tmp_path, lq, model_soi=True, eoi=13.0, clt=40.0)], n)
    o = Options(correct_pilot=False, correct_smoke=False, correct_soi=True)
    res = analyse(s, runs, o)
    maps = {w.target.name for w in res.plan.effective_writes}
    # the base maps read at 40 deg C; where the dynamic set gives the same
    # start the log can't tell them apart, so its warm-up maps move too --
    # never a warm map of either set
    assert maps & {"InjCrv_phiBas7_GMAP", "InjCrv_phiBas8_GMAP"}
    assert maps <= {"InjCrv_phiBas7_GMAP", "InjCrv_phiBas8_GMAP",
                    "InjCrv_phiDyn0_GMAP", "InjCrv_phiDyn1_GMAP", "InjCrv_phiDyn2_GMAP"}
    assert res.plan.ok
    o.tune_warmup = False
    off = analyse(s, runs, o)
    assert not [w for w in off.plan.effective_writes if w.target.name.startswith("InjCrv")]
    assert any(f.topic == "warmup" and "report only" in f.detail for f in off.findings)


@needs_ref
def test_warmup_rows_never_move_a_smoke_map_the_warm_engine_reads(lq, tmp_path):
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    sw = next(x for x in s.entities if x.name == "FlMng_swtqLimSmkQ_C")
    s.edit_cells(sw, [(0, 0, 0)])                       # lambda map 0 at every coolant temp
    try:
        def log(clt, name):
            rows = sim.wot_pull(sim.Car(s), clt=clt)
            for _t, v in rows:
                v["trq_smk"] = v["trq_final"] = v["trq_lim"] - 10
            return runs_from_paths([sim.write_log(str(tmp_path / name), s, n, [1, 8, 9, 10], rows)], n)
        o = Options(correct_pilot=False)
        warm = analyse(s, log(90.0, "w.csv"), o)
        assert [w for w in warm.plan.effective_writes if w.target.name.startswith("FlMng")]
        cold = analyse(s, log(35.0, "c.csv"), o)
        assert not [w for w in cold.plan.effective_writes if w.target.name.startswith("FlMng")]
        f = next(f for f in cold.findings if f.topic == "warmup")
        assert "warm rows only" in f.detail
    finally:
        s.undo()


@needs_ref
def test_warmup_report_splits_on_the_timing_selector(lq, tmp_path):
    import logtune_sim as sim
    from ovtsuite.logtune import analyse, runs_from_paths
    s, n = lq
    car = sim.Car(s)
    rows = []
    for k, clt in enumerate((15.0, 30.0, 50.0, 65.0, 85.0)):
        rows += sim.wot_pull(car, t0=k * 20.0, clt=clt)
    runs = runs_from_paths([sim.write_log(str(tmp_path / "wu.csv"), s, n, [1, 4, 7, 10, 74], rows)], n)
    f = next(f for f in analyse(s, runs).findings if f.topic == "warmup")
    assert [r[0] for r in f.table[1]] == ["10-21", "21-46", "46-61", "61-70"]


# ------------------------------------------------------------------ boost-pressure smoke limit
def _q_to_torque(fmtc, rpm, q):
    lo, hi = 0.0, 500.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if grid.lookup(fmtc, rpm, mid) < q:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


@needs_ref
def test_log_identifies_boost_pressure_smoke_limit_and_corrects_it(lq, tmp_path, lambda_space):
    """With space=1 the switches point at the lambda maps, but the logged smoke
    torque follows FlMng_qPresSmoke_MAP: the log wins, and that map is raised
    toward lambda 1.20 at the logged air -- capped per pass, never past the floor."""
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    from ovtsuite.logtune.analysis import PRES_SMOKE_MAP, STOICH
    s, n = lq
    E = {e.name: e for e in s.entities}
    qp, fm = E[PRES_SMOKE_MAP], E["FMTC_trq2qBas_MAP"]
    rows, t = [], 0.0
    for k in range(3):
        for rpm in range(1400, 2600, 60):
            boost = 1100 + (rpm - 1400) * 0.9
            q = grid.lookup(qp, rpm, boost)
            smk = _q_to_torque(fm, rpm, q)
            rows.append((t, {"rpm": float(rpm), "iq": q, "maf": q * STOICH * 1.45, "boost": boost,
                             "boost_spec": boost + 100, "n75": 70.0, "atm": 1010.0, "pedal": 70.0,
                             "clt": 85.0, "trq_drv": 400.0, "trq_lim": 345.0, "trq_smk": smk,
                             "trq_final": smk, "trq_gbx": 1000.0, "iat": 25.0}))
            t += 0.2
        t += 5.0
    p = sim.write_log(str(tmp_path / "takeoff.csv"), s, n, [1, 8, 9, 10], rows)
    o = Options(correct_pilot=False)
    res = analyse(s, runs_from_paths([p], n), o)
    assert any(f.topic == "smoke" and PRES_SMOKE_MAP in f.title for f in res.findings)
    assert not any("is not proven" in f.title for f in res.findings)
    ws = res.plan.effective_writes
    assert ws and {w.target.name for w in ws} == {PRES_SMOKE_MAP}
    for w in ws:
        if "non-decreasing" in w.reason:
            continue
        assert w.old < w.new <= w.old * (1 + o.smoke_max_step) + 0.05
    assert res.plan.ok


@needs_ref
def test_pres_smoke_correction_measures_the_injected_offset_without_torque_channels(lq, tmp_path, lambda_space):
    """A boost-preset log has no smoke-torque channel. The capped rows still
    sit a fixed distance above FlMng_qPresSmoke_MAP (the LQ injects +1.6 mg
    over it): that identifies the live table, and the correction aims each
    cell 1.6 mg below the fuel lambda 1.20 allows -- rich cells come down."""
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    from ovtsuite.logtune.analysis import PRES_SMOKE_MAP, STOICH
    s, n = lq
    qp = next(e for e in s.entities if e.name == PRES_SMOKE_MAP)
    OFF = 1.6
    rows, t = [], 0.0
    for k in range(3):
        for rpm in range(1100, 1700, 25):
            boost = 1000 + (rpm - 1100) * 0.8
            q = grid.lookup(qp, rpm, boost) + OFF
            rows.append((t, {"rpm": float(rpm), "iq": q, "maf": q * STOICH * 1.10, "boost": boost,
                             "atm": 950.0, "pedal": 75.0, "clt": 85.0, "boost_spec": boost + 600,
                             "n75": 80.0}))
            t += 0.2
        t += 5.0
    p = sim.write_log(str(tmp_path / "boost.csv"), s, n, [1, 10, 11], rows)
    res = analyse(s, runs_from_paths([p], n), Options(correct_pilot=False))
    assert any("identified from injected quantity" in f.title for f in res.findings), \
        [f.title for f in res.findings]
    ws = [w for w in res.plan.effective_writes if w.target.name == PRES_SMOKE_MAP]
    assert ws and all(w.new < w.old for w in ws if "non-decreasing" not in w.reason)
    for w in ws:
        if "non-decreasing" in w.reason:
            continue
        assert w.new >= w.old * (1 - 0.08) - 0.05            # one pass, capped step


@needs_ref
def test_pres_smoke_ignores_the_lagging_air_reading_right_after_a_pedal_step(lq, tmp_path, lambda_space):
    """The air-flow meter lags a tip-in: the first ~0.4 s read the old, low air
    against the new, capped fuel, so lambda looks ~1.02 when the engine is at
    the target. Those rows must not pull FlMng_qPresSmoke_MAP down. (Passes 3/4
    did, 2-3 mg below LQ stock in the take-off cells; cold take-offs crawled
    through them, 2026-10-12.)"""
    import logtune_sim as sim
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    from ovtsuite.logtune.analysis import PRES_SMOKE_MAP, STOICH
    s, n = lq
    qp = next(e for e in s.entities if e.name == PRES_SMOKE_MAP)
    OFF = 1.6
    rows, t = [], 0.0
    for k in range(12):
        rows.append((t, {"rpm": 1000.0, "iq": 12.0, "maf": 430.0, "boost": 960.0, "atm": 950.0,
                         "pedal": 0.0, "clt": 85.0, "boost_spec": 1000.0, "n75": 60.0}))
        t += 0.1
        for j, rpm in enumerate(range(1000, 1500, 25)):
            boost = 960 + (rpm - 1000) * 0.8
            q = grid.lookup(qp, rpm, boost) + OFF
            lam = 1.02 if j < 4 else 1.22                 # air reading settles after ~0.4 s
            rows.append((t, {"rpm": float(rpm), "iq": q, "maf": q * STOICH * lam, "boost": boost,
                             "atm": 950.0, "pedal": 70.0, "clt": 85.0, "boost_spec": boost + 600,
                             "n75": 90.0}))
            t += 0.1
        t += 5.0
    p = sim.write_log(str(tmp_path / "tipin.csv"), s, n, [1, 10, 11], rows)

    def downs(o):
        res = analyse(s, runs_from_paths([p], n), o)
        return [w for w in res.plan.effective_writes
                if w.target.name == PRES_SMOKE_MAP and w.new < w.old - 0.05]

    assert downs(Options(correct_pilot=False, maf_settle_s=0.0)), "the test must provoke a cut"
    assert downs(Options(correct_pilot=False)) == []


# ------------------------------------------------------- dynamic SOI set
def _dyn_log(tmp_path, lq, groups, smoke_torque=5000.0, soi_from=None, name="d.csv"):
    import logtune_sim as sim
    s, n = lq
    car = sim.Car(s)
    rows = []
    for k in range(3):
        rows += sim.wot_pull(car, t0=k * 20.0, eoi=13.0, smoke_torque=smoke_torque)
    if soi_from is not None:                       # the ECU on another SOI set
        from ovtsuite import eoi
        calc = eoi.Calculator(eoi.models(s)[0], soi_set=soi_from)
        for _t, v in rows:
            v["soi"] = calc.cell(90.0, v["rpm"], v["iq"]).soi_btdc
            v["dur"] = v["soi"] + 13.0
    return sim.write_log(str(tmp_path / name), s, n, groups, rows)


@needs_ref
def test_smoke_limited_rows_correct_the_dynamic_soi_set(lq, tmp_path):
    """With the torques logged, the ECU's own rule (request and torque limit
    over the smoke limit by InjCrv_trqDyn*High_C) puts smoke-limited rows on
    InjCrv_phiDyn*_GMAP -- the base maps are not what the ECU read there."""
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    o = Options(correct_pilot=False, correct_smoke=False, correct_soi=True)
    groups = [1, 4, 7, 8, 10, 11, 74]
    runs = runs_from_paths([_dyn_log(tmp_path, lq, groups, smoke_torque=100.0)], n)
    if not all(r.has("trq_smk") and r.has("trq_lim") and r.has("trq_drv") for r in runs):
        pytest.skip("the LQ's blocks %s don't carry the three torques" % groups)
    res = analyse(s, runs, o)
    maps = {w.target.name for w in res.plan.effective_writes if w.target.name.startswith("InjCrv_phi")}
    assert maps and all(m.startswith("InjCrv_phiDyn") for m in maps), maps
    # not smoke-limited: the base maps, as before
    runs = runs_from_paths([_dyn_log(tmp_path, lq, groups, name="b.csv")], n)
    res = analyse(s, runs, o)
    maps = {w.target.name for w in res.plan.effective_writes if w.target.name.startswith("InjCrv_phi")}
    assert maps and all(m.startswith("InjCrv_phiBas") for m in maps), maps


@needs_ref
def test_without_torques_the_logged_soi_names_the_set(lq, tmp_path):
    """No torques logged: a logged start that matches the dynamic set's (and
    not the base set's) start puts the row on the dynamic set."""
    from ovtsuite.logtune import Options, analyse, runs_from_paths
    s, n = lq
    o = Options(correct_pilot=False, correct_smoke=False, correct_soi=True)
    runs = runs_from_paths([_dyn_log(tmp_path, lq, [1, 4, 7, 10, 74], soi_from="dyn")], n)
    if any(r.has("trq_smk") for r in runs):
        pytest.skip("these blocks carry the smoke limit")
    res = analyse(s, runs, o)
    maps = {w.target.name for w in res.plan.effective_writes if w.target.name.startswith("InjCrv_phi")}
    # low speed, where the sets differ by degrees: the dynamic maps; high
    # speed, where they agree within a degree: both sets
    assert any(m.startswith("InjCrv_phiDyn") for m in maps), maps
    dyn_low = [w for w in res.plan.effective_writes if w.target.name.startswith("InjCrv_phiDyn")
               and w.target.x_values()[w.col] <= 2000]
    bas_low = [w for w in res.plan.effective_writes if w.target.name.startswith("InjCrv_phiBas")
               and w.target.x_values()[w.col] <= 1500]
    assert dyn_low and not bas_low

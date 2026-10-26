# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Injector setup (ovtsuite.injectors): characteristics read from stock
files, the shipped presets, identification, and fitting (duration set,
centre-kept timing, solenoid calibration, lean start)."""

import contextlib
import io
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import eoi  # noqa: E402
from ovtsuite import injectors as inj  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
REF = os.path.join(ROOT, "Reference")


def _session(*parts):
    p = os.path.join(REF, *parts)
    if not os.path.exists(p):
        pytest.skip("%s not present" % parts[-1])
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return TunerSession(p)


def _model(s):
    ms = eoi.models(s)
    assert ms, "no injection model"
    return ms[0]


@pytest.fixture(scope="module")
def cat():
    c = inj.catalogue()
    if not len(c):
        pytest.skip("no preset data installed")
    return c


# ------------------------------------------------------------ data model
def test_characteristic_round_trips_and_blends_like_the_ecu():
    t0 = inj.RawTable([1000, 2000], [10, 20], [[2, 4], [3, 5]])
    t1 = inj.RawTable([1000, 2000], [10, 20], [[4, 8], [6, 10]])
    c = inj.Characteristic([0, 20], [1, 0], [t0, t1])
    # 10 deg BTDC -> selector 0.5: half of each table
    assert c.duration(10, 1500, 15) == pytest.approx((3.5 + 7.0) / 2)
    back = inj.Characteristic.from_json(c.to_json())
    assert back.duration(10, 1500, 15) == pytest.approx(c.duration(10, 1500, 15))
    # a derived characteristic reads the quantity divided by its flow factor
    assert c.scaled(2.0).duration(20, 1000, 20) == pytest.approx(c.duration(20, 1000, 10))


def test_presets_cover_the_hardware_sets(cat):
    for pid in ("pd100", "pd100-edc15", "pd-al-1043", "pd-ba-late", "brm", "bhw", "bpw"):
        assert cat.get(pid) is not None, pid
    # aftermarket presets resolve against their base, lean by construction
    base = cat.char_of(cat.get("pd-al-1043"))
    firad = cat.char_of(cat.get("firad-hf50"))
    assert firad.duration(10, 3000, 45) < base.duration(10, 3000, 45)
    assert firad.duration(10, 3000, 45) == pytest.approx(base.duration(10, 3000, 30))


def test_mg_per_degree_of_the_factory_sets(cat):
    want = {"pd100": 2.06, "pd-al-1043": 2.55, "pd-ba-late": 2.38, "brm": 1.67,
            "bhw": 2.38, "bpw": 2.49}
    for pid, mpd in want.items():
        c = cat.char_of(cat.get(pid))
        assert c.mg_per_degree(10, 3000, 45) == pytest.approx(mpd, abs=0.02), pid


# ------------------------------------------------------------ identify
def test_stock_files_identify_as_their_own_preset(cat):
    for parts, pid in ((("EDC16-SGO-Unpacked", "038906016AB_7989.bin"), "pd100"),
                       (("Binaries", "038906016N.bin"), "pd100"),
                       (("EDC16-SGO-Unpacked", "03G906016LQ_1717.bin"), "bpw"),
                       (("Binaries", "EDC15", "038906019KP_AXR_100hp_366912.ori"),
                        "pd100-edc15"),
                       (("Binaries", "EDC15", "038906019FE_ARL_150hp_362470.bin"),
                        "pd-al-1043")):
        hits = inj.identify(inj.characteristic(_model(_session(*parts))), cat, 2)
        assert hits[0].preset.id == pid and hits[0].same, (parts, hits[0].preset.id)
        if pid != "pd-al-1043":         # ARL vs the ASZ source: 1 deg locally
            assert hits[0].exact, parts


def test_pd100_shares_its_bip_curve_across_platforms(cat):
    p = cat.get("pd100")
    a, b = p.electrical["BIP_tiBase_CUR"], p.electrical["zmwBPGndKL"]
    assert [round(v) for v in a.z[0]] == [round(v) for v in b.z[0]]


# ------------------------------------------------------------ fitting
def test_fit_bhw_onto_bpw_matches_the_bhw_calibration(cat):
    s = _session("EDC16-SGO-Unpacked", "03G906016LQ_1717.bin")
    m = _model(s)
    p = cat.get("bhw")
    plan = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(body=True),
                        cat.electrical_of(p), {}, inj.platform_for(s))
    assert not plan.errors
    names = {c.entity.name for c in plan.changes}
    # the parameters the hand-made BHW -> LQ port moved
    assert {"InjVCD_tiFastDecayMI1_C", "BIP_facInjBegCor_MAP",
            "InjVlv_tiMI1BreMin_MAP"} <= names
    inj.apply_fit(s, [plan], "test")
    hits = inj.identify(inj.characteristic(_model(s)), cat, 1)
    assert hits[0].preset.id == "bhw" and hits[0].worst < 0.05


def test_nozzles_only_keeps_the_solenoid_calibration(cat):
    s = _session("Binaries", "EDC15", "038906019AT_ATD_100hp_366203.ori")
    m = _model(s)
    p = cat.get("pd-al-1043")
    plan = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(body=False),
                        cat.electrical_of(p), {}, inj.platform_for(s))
    assert not any(c.label == "solenoid calibration" for c in plan.changes)
    full = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(body=True),
                        cat.electrical_of(p), {}, inj.platform_for(s))
    assert any(c.entity.name == "zmwBPGndKL" for c in full.changes)
    # one write per parameter, even with an alias in the preset
    assert sum(c.entity.name == "zmwBPGndKL" for c in full.changes) == 1


def test_centre_kept_timing_follows_bosch_for_atd_to_asz(cat):
    """ATD (PD100) with 1043 injectors, centre kept, against Bosch's own ASZ
    calibration: the start of injection lands within ~2.6 deg RMS, and keeping
    the start instead is clearly worse (the research figures)."""
    asz = eoi.Calculator(_model(_session("Binaries", "EDC15",
                                         "038906019HJ_ASZ_130hp_363142.bin")))
    p = cat.get("pd-al-1043")

    def rms(policy):
        s = _session("Binaries", "EDC15", "038906019AT_ATD_100hp_366203.ori")
        m = _model(s)
        plan = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(timing=policy),
                            cat.electrical_of(p), {}, inj.platform_for(s))
        inj.apply_fit(s, [plan], "t")
        c = eoi.Calculator(_model(s))
        err = [c.cell(90.0, n, q).soi_btdc - asz.cell(90.0, n, q).soi_btdc
               for n in (1500, 2000, 2500, 3000, 3500, 4000) for q in (10, 20, 30, 40, 50)]
        return math.sqrt(sum(e * e for e in err) / len(err))

    centre, keep = rms(inj.KEEP_CENTRE), rms(inj.KEEP_SOI)
    assert centre < 2.8
    assert keep > centre + 0.5


def test_bigger_nozzles_shorten_the_duration_and_retard_the_start(cat):
    s = _session("EDC16-SGO-Unpacked", "038906016AB_7989.bin")
    m = _model(s)
    before = eoi.Calculator(m).cell(90.0, 3000, 40)
    p = cat.get("firad-hf50")
    plan = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(body=False),
                        {}, {}, inj.platform_for(s))
    assert not plan.errors
    inj.apply_fit(s, [plan], "t")
    after = eoi.Calculator(_model(s)).cell(90.0, 3000, 40)
    assert after.duration < before.duration - 2.0
    assert after.soi_btdc < before.soi_btdc
    # the centre of injection stays (within the table resolution)
    c0 = (before.eoi - before.soi_btdc) / 2
    c1 = (after.eoi - after.soi_btdc) / 2
    assert abs(c1 - c0) < 0.6


def test_user_preset_from_a_file_fits_back_exactly(cat):
    s = _session("EDC16-SGO-Unpacked", "038997016K__0811.bin")
    m = _model(s)
    pr = inj.preset_from_session(s, m, inj.platform_for(s), "user-bhw", "My BHW")
    back = inj.Preset.from_json(pr.to_json())
    mean, worst = inj.compare(back.char, inj.characteristic(m))
    assert worst == pytest.approx(0.0, abs=1e-9)
    assert "BIP_tiBase_CUR" in back.electrical


def test_stock_check_finds_the_tuned_q_and_passes_stock_files():
    q = _session("Binaries", "038906016Q.Bin")
    assert any("lambda probe" in t for t in inj.tuned_signs(q))
    for parts in (("EDC16-SGO-Unpacked", "038906016AB_7989.bin"),
                  ("Binaries", "03G906016GC.bin")):       # OEM, probe off, paths kept
        assert inj.tuned_signs(_session(*parts)) == [], parts


# ------------------------------------------------------------ several datasets
def test_every_dataset_identified_and_disagreement_said():
    """identify_datasets answers per dataset; datasets_disagree is None when
    they agree and names each one's hardware when they do not."""
    t0 = inj.RawTable([1000, 2000], [10, 20], [[2, 4], [3, 5]])
    t1 = inj.RawTable([1000, 2000], [10, 20], [[4, 8], [6, 10]])
    a = inj.Characteristic([0, 20], [1, 0], [t0, t1])
    b = a.scaled(1.6)
    c = inj.Catalogue([inj.Preset("a", "Set A", char=a), inj.Preset("b", "Set B", char=b)])

    class M:
        def __init__(self, label, ch):
            self.label, self.ch = label, ch
    import unittest.mock as um
    with um.patch.object(inj, "characteristic", lambda m: m.ch):
        same = inj.identify_datasets([M("Automatic", a), M("Manual 1", a)], c, 2)
        diff = inj.identify_datasets([M("Automatic", a), M("Manual 1", b)], c, 2)
    assert [r.label for r in same] == ["Automatic", "Manual 1"]
    assert all(r.top.preset.id == "a" and r.top.exact for r in same)
    assert inj.datasets_disagree(same) is None
    assert inj.datasets_disagree(same[:1]) is None
    why = inj.datasets_disagree(diff)
    assert why and "Automatic: Set A" in why and "Manual 1: Set B" in why


def test_gq_datasets_agree(cat):
    s = _session("Binaries", "EDC15", "038906019GQ.bin")
    ms = [m for m in eoi.models(s) if not inj._missing_duration(m)]
    res = inj.identify_datasets(ms, cat, 2)
    assert [r.label for r in res] == ["Automatic", "Manual 1", "Manual 2"]
    assert len({r.top.preset.id for r in res}) == 1
    assert inj.datasets_disagree(res) is None


# ------------------------------------------------------------ start maps, again
def _start_cells(model):
    """The start (cranking) set's tables, by name."""
    ss = next(x for x in model.soi_sets if x.key == "start")
    return {e.name: e for e in ss.set.tables if e is not None}


@pytest.mark.parametrize("parts", [
    ("EDC16-SGO-Unpacked", "038906016AB_7989.bin"),                  # EDC16 BEW
    ("Binaries", "EDC15", "038906019AT_ATD_100hp_366203.ori"),       # EDC15 ATD
])
def test_start_maps_follow_the_new_duration(cat, parts):
    """PD100 -> ASZ / ARL (1043) injectors: the start (cranking) SOI maps,
    engine speed x coolant, are retimed like the running maps -- the centre
    of injection at the start quantity stays -- and left alone (said so)
    when unticked."""
    s = _session(*parts)
    m = _model(s)
    p = cat.get("pd-al-1043")
    names = set(_start_cells(m))
    plan = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(), cat.electrical_of(p),
                        {}, inj.platform_for(s))
    assert not plan.errors
    got = {c.entity.name for c in plan.changes if c.label == "start timing"}
    assert got == names
    assert any("Start (cranking)" in f.text and "retimed" in f.text for f in plan.findings)
    off = inj.plan_fit(s, m, cat.char_of(p), inj.FitOptions(start=False),
                       cat.electrical_of(p), {}, inj.platform_for(s))
    assert not {c.entity.name for c in off.changes} & names
    assert any("left as it is" in f.text for f in off.findings)
    # the centre of injection at the start quantity stays put (40 mg, or the
    # ECU's start-quantity map: EDC15 mrwSTMGRKF) -- where anything is
    # injected: a 0 mg cell (ATD at 200 rpm, 20 degC) is left as it is
    before = eoi.Calculator(m, soi_set="start")
    pts = [(n, t) for n in (200.0, 500.0, 900.0) for t in (-10.0, 20.0, 80.0)
           if before.dur_q(t, n, t) >= 0.5]
    assert len(pts) >= 6
    tab = next(iter(_start_cells(m).values()))
    c0 = []
    for n, t in pts:
        cell = eoi.Table(tab).at(n, t)
        d = before.duration(cell, n, before.dur_q(t, n, t))[0]
        c0.append(before.btdc(cell) - d / 2)
    inj.apply_fit(s, [plan], "t")
    m1 = _model(s)
    after = eoi.Calculator(m1, soi_set="start")
    tab1 = _start_cells(m1)[tab.name]
    for (n, t), c in zip(pts, c0):
        cell = eoi.Table(tab1).at(n, t)
        d = after.duration(cell, n, after.dur_q(t, n, t))[0]
        assert after.btdc(cell) - d / 2 == pytest.approx(c, abs=0.1), (n, t)


def test_a_plan_made_again_after_apply_writes_nothing(cat):
    """Apply rounds every value to the file's raw step; planning the same
    hardware again must not count that rounding as a change (the window's OK
    would ask to write the tables a second time)."""
    s = _session("EDC16-SGO-Unpacked", "038906016AB_7989.bin")
    p = cat.get("pd-al-1043")

    def plan():
        return inj.plan_fit(s, _model(s), cat.char_of(p), inj.FitOptions(),
                            cat.electrical_of(p), cat.extras_of(p), inj.platform_for(s))
    first = plan()
    assert first.n_cells > 1000
    inj.apply_fit(s, [first], "t")
    again = plan()
    assert again.n_cells == 0, [(c.entity.name, c.cells) for c in again.changes]

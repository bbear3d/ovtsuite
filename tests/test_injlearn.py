# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Injector delivery learned from logs (ovtsuite.injlearn): the lambda-probe
method, the before/after same-work method, and the plan they produce."""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import eoi  # noqa: E402
from ovtsuite import injectors as inj  # noqa: E402
from ovtsuite import injlearn as L  # noqa: E402
from ovtsuite.logtune.samples import Run  # noqa: E402

REF = os.path.join(os.path.dirname(__file__), "..", "Reference")


def _session(*parts):
    p = os.path.join(REF, *parts)
    if not os.path.exists(p):
        pytest.skip("%s not present" % parts[-1])
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return TunerSession(p)


def _run(points, k=1.0, lam=1.6, rows=12, clt=88.0, extra=None):
    """Steady blocks of rows at each (rpm, iq[, vss, gear]); the hardware
    delivers k x iq; maf follows from lambda."""
    r = Run("synthetic")
    cols = {c: [] for c in ("rpm", "iq", "maf", "lambda", "clt", "vss", "gear", "pedal")}
    t = 0.0
    for p in points:
        n, q = p[0], p[1]
        v = p[2] if len(p) > 2 else None
        g = p[3] if len(p) > 3 else None
        for _ in range(rows):
            r.t.append(t)
            t += 0.2
            cols["rpm"].append(float(n))
            cols["iq"].append(float(q))
            cols["lambda"].append(lam if lam else None)
            cols["maf"].append(q * k * 14.5 * lam if lam else None)
            cols["clt"].append(clt)
            cols["vss"].append(v)
            cols["gear"].append(g)
            cols["pedal"].append(0.0 if v == 0 else 20.0)
    r.data = cols
    if extra:
        r.data.update(extra)
    return r


@pytest.fixture(scope="module")
def lq():
    s = _session("EDC16-SGO-Unpacked", "03G906016LQ_1717.bin")
    return s, eoi.models(s)[0]


def test_lambda_method_finds_the_delivery_factor():
    opts = L.LearnOptions()
    run = _run([(2000, 20), (2500, 30), (3000, 40)], k=1.10)
    ss = L.lambda_samples([run], opts)
    assert ss and all(abs(s.k - 1.10) < 1e-9 for s in ss)
    # the first and last row of a block are not steady; cold rows are dropped
    assert not L.lambda_samples([_run([(2000, 20)], k=1.1, clt=40.0)], opts)
    # a rich or no-probe reading is not used
    assert not L.lambda_samples([_run([(2000, 20)], k=1.1, lam=0.95)], opts)


def test_same_work_pairs_idle_and_road_points():
    opts = L.LearnOptions()
    before = _run([(850, 6.0, 0, 0), (1800, 14.0, 80, 5)], lam=None)
    after = _run([(850, 5.0, 0, 0), (1800, 12.0, 80, 5), (2200, 16.0, 100, 5)], lam=None)
    ss, notes = L.same_work_samples([before], [after], opts)
    got = {s.label: s.k for s in ss}
    assert got["idle"] == pytest.approx(6.0 / 5.0)
    assert got["80 km/h in gear 5"] == pytest.approx(14.0 / 12.0)
    assert "100 km/h in gear 5" not in got           # no "before" there
    # different gear at the same speed: not the same point
    other = _run([(1500, 12.0, 80, 6)], lam=None)
    assert not L.same_work_samples([before], [other], opts)[0]


def test_learn_plans_a_duration_correction(lq):
    s, m = lq
    run = _run([(n, q) for n in (1750, 2000, 2250, 2500, 2750, 3000)
                for q in (20, 25, 30, 35, 40)], k=1.08)
    rep = L.learn(s, m, [], runs_after=[run], runs_before=[])
    assert rep.changed > 0 and rep.unmeasured > 0
    assert rep.plan is not None and rep.plan.cells
    assert not [f for f in rep.plan.findings if f.level == "error"]
    # where measured, the new duration at q is the old one at q / 1.08
    old = inj.characteristic(m)
    pts = {(n, q): k for n, q, k, _w in rep.points}
    for (n, q), k in pts.items():
        if 2000 <= n <= 2750 and 25 <= q <= 35:
            assert k == pytest.approx(1.08, abs=1e-6)
    t0 = old.tables[0]
    e0 = m.duration.tables[0]
    tab = eoi.Table(e0)
    cells = rep.plan.cells.get(id(e0), (e0, {}))[1]
    hit = 0
    for i_n, n in enumerate(t0.rpm):
        for i_q, q in enumerate(t0.q):
            v = cells.get(tab.cell_of(i_n, i_q))
            if v is None or (n, q) not in pts or abs(pts[(n, q)] - 1.08) > 1e-6:
                continue
            assert v == pytest.approx(t0.at(n, q / 1.08), abs=0.05)
            assert v < t0.z[i_n][i_q]
            hit += 1
    assert hit, "no measured cell landed in table 0"
    js = rep.to_json()
    assert js["samples"]["lambda_rows"] > 0 and js["maps"]


def test_learn_holds_a_large_correction_to_one_step(lq):
    s, m = lq
    run = _run([(n, q) for n in (2000, 2500) for q in (25, 30)], k=1.5)
    rep = L.learn(s, m, [], runs_after=[run], runs_before=[])
    assert max(k for _n, _q, k, _w in rep.points) == pytest.approx(1.15)
    assert any("held" in n for n in rep.notes)


def test_learn_without_measurement_writes_nothing(lq):
    s, m = lq
    run = _run([(2000, 20)], lam=None)
    rep = L.learn(s, m, [], runs_after=[run], runs_before=[])
    assert rep.plan is None and rep.notes


def test_edc15_learns_from_before_and_after():
    s = _session("Binaries", "EDC15", "038906019AT_ATD_100hp_366203.ori")
    m = eoi.models(s)[0]
    before = _run([(900, 7.0, 0, 0), (1900, 15.0, 90, 5)], lam=None, rows=20)
    after = _run([(900, 6.3, 0, 0), (1900, 13.6, 90, 5)], lam=None, rows=20)
    rep = L.learn(s, m, [], runs_after=[after], runs_before=[before])
    assert rep.used["same_work_points"] == 2
    assert rep.plan is not None and rep.changed > 0


def test_a_logs_ident_names_its_datasets(tmp_path):
    """A log is recorded while one dataset runs: the ident a VCDS-layout log
    records names it -- or the datasets sharing it (Manual 1 and Manual 2
    differ by coding only); a log with none is taken for every dataset."""
    s = _session("Binaries", "EDC15", "038906019GQ.bin")
    labels = ["Automatic", "Manual 1", "Manual 2"]
    ag, sg = "038906019GQ 1,9l R4 EDC  AG  1524", "038906019GQ 1,9l R4 EDC  SG  1524"
    assert L.datasets_for_identity(s, ag) == ["Automatic"]
    # sent in fixed-length blocks that may split a word: whitespace is ignored
    assert L.datasets_for_identity(s, "038906019GQ 1,9l R4 ED C  SG  1524") == \
        ["Manual 1", "Manual 2"]
    assert L.datasets_for_identity(s, "") == []
    assert L.datasets_for_identity(s, "038906019AT 1,9l R4 EDC  SG  1524") == []
    got = L.log_datasets(s, labels, [ag, sg, "", "03G906016LQ"])
    assert got == [(["Automatic"], "ident"), (["Manual 1", "Manual 2"], "ident"),
                   (labels, "unknown"), (labels, "unknown")]
    # a dataset not asked for is not one a log applies to
    assert L.log_datasets(s, ["Manual 1"], [ag]) == [(["Manual 1"], "unknown")]

    sys.path.insert(0, os.path.dirname(__file__))
    import logtune_sim as sim
    from ovtsuite.datalog import logfile as lf
    from ovtsuite.datalog.channels import ChannelNamer
    rows = [(i * 0.2, {"rpm": 900.0, "iq": 7.0, "clt": 88.0, "vss": 0.0}) for i in range(10)]
    a = sim.write_log(str(tmp_path / "a.csv"), s, ChannelNamer(s), [1, 6], rows)
    log = lf.load(a)
    log.identity = ag
    b = str(tmp_path / "b.csv")
    lf.write_vcds(b, log)
    (pa, ia, ra), (pb, ib, rb) = L.read_logs(s, [a, b])
    assert (pa, ia, pb, ib) == (a, "", b, ag)
    assert ra.has("rpm") and rb.has("iq") and len(rb) == 10

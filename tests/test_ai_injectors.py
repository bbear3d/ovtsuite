# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The assistant's injector-setup tools (ovtsuite.ai.injector_tools): the
same answers as Tools > Injector setup, and changes that pass check_changes."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ovtsuite.ai import proposals as P  # noqa: E402
from ovtsuite.ai.tools import Context, default_registry  # noqa: E402
from ovtsuite.ai.workspace import FileWorkspace  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")
ATD = os.path.join(ROOT, "Reference", "Binaries", "EDC15", "038906019AT_ATD_100hp_366203.ori")
GQ = os.path.join(ROOT, "Reference", "Binaries", "EDC15", "038906019GQ.bin")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    os.environ[P.ENV_DIR] = str(tmp_path_factory.mktemp("ai"))
    reg = default_registry()
    ctx = Context(FileWorkspace(), client="pytest")
    yield reg, ctx
    os.environ.pop(P.ENV_DIR, None)


def _open(env, path):
    if not os.path.isfile(path):
        pytest.skip("needs " + os.path.basename(path))
    reg, ctx = env
    ok, r = reg.call("open_file", ctx, {"path": path})
    assert ok, r
    return r["opened"][0]["handle"]


def call(env, name, **args):
    reg, ctx = env
    ok, r = reg.call(name, ctx, args)
    assert ok, r
    json.dumps(r)
    return r


def test_registered_read_only(env):
    reg, _ = env
    for n in ("injector_hardware", "injector_presets", "injector_setup_changes",
              "injector_learn_report"):
        assert reg.get(n) is not None and reg.get(n).read_only, n


def test_presets_and_hardware(env):
    r = call(env, "injector_presets")
    ids = {p["id"] for p in r["presets"]}
    assert {"pd100", "bhw", "bpw", "brm", "pd-al-1043"} <= ids
    assert call(env, "injector_presets", kind="aftermarket")["count"] >= 5
    h = _open(env, ATD)
    hw = call(env, "injector_hardware", handle=h)
    assert hw["matches"][0]["preset"] == "pd100-edc15" and hw["matches"][0]["exact"]


def test_setup_changes_pass_check_changes(env):
    h = _open(env, LQ)
    r = call(env, "injector_setup_changes", handle=h, preset="bhw")
    assert r["changes"] and r["complete_injectors"]
    chk = call(env, "check_changes", handle=h, changes=r["changes"])
    assert chk["ok"], chk["problems"]


def test_learn_report_from_a_log(env, tmp_path):
    import logtune_sim as sim
    from ovtsuite.datalog.channels import ChannelNamer
    h = _open(env, LQ)
    import contextlib
    import io
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()):
        s = TunerSession(LQ)           # only to lay the log out on its measuring blocks
    rows, t = [], 0.0
    for n in (2000, 2500, 3000):
        for q in (20.0, 30.0):
            for _ in range(15):     # the hardware delivers 6 % more than asked
                rows.append((t, {"rpm": float(n), "iq": q, "maf": q * 1.06 * 14.5 * 1.5,
                                 "lambda": 1.5, "clt": 88.0, "dur": 10.0}))
                t += 0.2
    path = sim.write_log(str(tmp_path / "after.csv"), s, ChannelNamer(s), [1, 3, 40], rows)
    r = call(env, "injector_learn_report", handle=h, after=[path])
    assert r["timing"] == "centre"
    assert r["samples"]["lambda_rows"] > 0
    assert r["breakpoints_corrected"] > 0 and r["changes"]
    assert all(f["factor"] == pytest.approx(1.06, abs=0.01) for f in r["factors"]
               if 2000 <= f["rpm"] <= 3000 and 20 <= f["quantity"] <= 30)
    chk = call(env, "check_changes", handle=h, changes=r["changes"])
    assert chk["ok"], chk["problems"]
    # the start of injection follows the timing asked for, as in the window
    k = call(env, "injector_learn_report", handle=h, after=[path], timing="soi")
    assert k["timing"] == "soi" and len(k["changes"]) < len(r["changes"])
    reg, ctx = env
    ok, bad = reg.call("injector_learn_report", ctx, {"handle": h, "after": [path],
                                                      "timing": "late"})
    assert not ok and "timing" in bad["error"]


def test_every_dataset_identified_and_fitted(env):
    """An EDC15 file with three datasets: each identified (they agree), and
    the setup changes cover them all unless one is named."""
    h = _open(env, GQ)
    hw = call(env, "injector_hardware", handle=h)
    assert [d["dataset"] for d in hw["per_dataset"]] == ["Automatic", "Manual 1", "Manual 2"]
    assert hw["disagree"] is None
    assert hw["matches"] == hw["per_dataset"][0]["matches"]
    one = call(env, "injector_hardware", handle=h, dataset="Manual 1")
    assert one["dataset"] == "Manual 1" and "per_dataset" not in one
    r = call(env, "injector_setup_changes", handle=h, preset="bpw")
    assert r["datasets"] == ["Automatic", "Manual 1", "Manual 2"]
    assert [x["dataset"] for x in r["per_dataset"]] == r["datasets"]
    assert all(x["cells"] > 0 for x in r["per_dataset"])
    m1 = call(env, "injector_setup_changes", handle=h, preset="bpw", dataset="Manual 1")
    assert m1["dataset"] == "Manual 1" and 0 < len(m1["changes"]) < len(r["changes"])
    chk = call(env, "check_changes", handle=h, changes=r["changes"])
    assert chk["ok"], chk["problems"]


def _gq_logs(tmp_path):
    """Before / after logs on the GQ (no lambda probe: idle and a road
    speed, the new hardware delivering 1/0.9 of what is asked), in the
    native layout (no ident) and as VCDS logs carrying the AG / SG ident."""
    import contextlib
    import io
    import logtune_sim as sim
    from ovtsuite.datalog import logfile as lf
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession
    if not os.path.isfile(GQ):
        pytest.skip("needs the GQ")
    with contextlib.redirect_stdout(io.StringIO()):
        s = TunerSession(GQ)

    def rows(f):
        out, t = [], 0.0
        for n, q, v in ((900, 7.0, 0.0), (1900, 15.0, 90.0)):
            for _ in range(20):
                out.append((t, {"rpm": float(n), "iq": q * f, "clt": 88.0, "vss": v,
                                "pedal": 0.0 if v == 0 else 20.0}))
                t += 0.2
        return out
    got = {}
    for name, f in (("before", 1.0), ("after", 0.9)):
        p = sim.write_log(str(tmp_path / (name + ".csv")), s, ChannelNamer(s), [1, 6], rows(f))
        got[name] = p
        for box in ("AG", "SG"):
            log = lf.load(p)
            log.identity = "038906019GQ 1,9l R4 EDC  %s  1524" % box
            q = str(tmp_path / ("%s_%s.csv" % (name, box.lower())))
            lf.write_vcds(q, log)
            got["%s_%s" % (name, box.lower())] = q
    return got


def test_learn_report_for_every_dataset(env, tmp_path):
    """An EDC15 file with three datasets: with none named, each learns from
    the logs that apply to it; logs with no ident are taken for every
    dataset (as the window does) and the result says so."""
    lg = _gq_logs(tmp_path)
    h = _open(env, GQ)
    r = call(env, "injector_learn_report", handle=h, after=[lg["after"]],
             before=[lg["before"]])
    labels = ["Automatic", "Manual 1", "Manual 2"]
    assert r["dataset"] is None and r["datasets"] == labels
    assert [x["dataset"] for x in r["per_dataset"]] == labels
    assert all(x["breakpoints_corrected"] > 0 and x["cells"] > 0 for x in r["per_dataset"])
    assert all(x["after_logs"] == ["after.csv"] and x["before_logs"] == ["before.csv"]
               for x in r["per_dataset"])
    assert r["breakpoints_corrected"] == sum(x["breakpoints_corrected"]
                                             for x in r["per_dataset"])
    assert r["samples"] == r["per_dataset"][0]["samples"]
    assert {x["how"] for x in r["log_datasets"]} == {"unknown"}
    assert "every dataset" in r["notes"][0]
    assert r["findings"] and all(f["text"].split(": ")[0] in labels for f in r["findings"])
    assert {m["dataset"] for m in r["maps"]} == set(labels)
    assert all(abs(p["delivery_factor"] - 1 / 0.9) < 0.01 for p in r["same_work_points"])
    chk = call(env, "check_changes", handle=h, changes=r["changes"])
    assert chk["ok"], chk["problems"]
    # one dataset named: every log for it, the result as before
    one = call(env, "injector_learn_report", handle=h, after=[lg["after"]],
               before=[lg["before"]], dataset="Manual 1", timing="soi")
    assert one["dataset"] == "Manual 1" and one["timing"] == "soi"
    assert "per_dataset" not in one and "log_datasets" not in one
    assert 0 < len(one["changes"]) < len(r["changes"])


def test_learn_report_follows_the_logs_ident(env, tmp_path):
    """A log carrying the ECU's ident was recorded on that dataset: an AG log
    corrects the automatic dataset only; an SG log both manual datasets (they
    share the ident), with a note that it cannot tell them apart."""
    lg = _gq_logs(tmp_path)
    h = _open(env, GQ)
    r = call(env, "injector_learn_report", handle=h, after=[lg["after_ag"]],
             before=[lg["before_ag"]])
    per = {x["dataset"]: x for x in r["per_dataset"]}
    assert per["Automatic"]["breakpoints_corrected"] > 0
    assert "skipped" in per["Manual 1"] and "skipped" in per["Manual 2"]
    assert r["log_datasets"] == [{"log": "after_ag.csv", "datasets": ["Automatic"],
                                  "how": "ident"},
                                 {"log": "before_ag.csv", "datasets": ["Automatic"],
                                  "how": "ident"}]
    assert {m["dataset"] for m in r["maps"]} == {"Automatic"}
    assert not any("every dataset" in n for n in r["notes"])
    m = call(env, "injector_learn_report", handle=h, after=[lg["after_sg"]],
             before=[lg["before_sg"]])
    per = {x["dataset"]: x for x in m["per_dataset"]}
    assert "skipped" in per["Automatic"]
    assert per["Manual 1"]["cells"] > 0 and per["Manual 2"]["cells"] > 0
    assert any("Manual 1 and Manual 2 share one ident" in n for n in m["notes"])
    # the dataset named is learned from every log, with a word when a log's
    # ident says the car ran another one
    one = call(env, "injector_learn_report", handle=h, after=[lg["after_ag"]],
               before=[lg["before"]], dataset="Manual 1")
    assert one["dataset"] == "Manual 1" and one["changes"]
    assert any("after_ag.csv records the ident of another dataset" in n for n in one["notes"])


def test_propose_carries_the_hardware_record(env):
    h = _open(env, LQ)
    r = call(env, "propose_injector_setup", handle=h, preset="bhw",
             rationale="pytest")
    from ovtsuite.ai import proposals as P
    prop = P.load(r["proposal"])
    assert prop["hardware"]["kind"] == "pd_injectors"
    assert prop["hardware"]["parts"] == {"preset": "bhw", "complete": True}
    assert "Injector setup" in prop["title"]
    P.remove(r["proposal"])


def test_in_a_project_the_record_and_the_logs_since_the_change(env, tmp_path):
    """injector_hardware says what the project records as fitted (and that
    this version is not set up for it yet); injector_learn_report takes the
    logs added since the recorded change."""
    import contextlib
    import io
    import logtune_sim as sim
    from ovtsuite import project as ovt
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.project import hardware as HW
    from ovtsuite.session import TunerSession
    if not os.path.isfile(LQ):
        pytest.skip("needs the LQ")
    with contextlib.redirect_stdout(io.StringIO()):
        s = TunerSession(LQ)
    rows, t = [], 0.0
    for n in (2000, 2500, 3000):
        for q in (20.0, 30.0):
            for _ in range(15):
                rows.append((t, {"rpm": float(n), "iq": q, "maf": q * 1.06 * 14.5 * 1.5,
                                 "lambda": 1.5, "clt": 88.0, "dur": 10.0}))
                t += 0.2
    log = sim.write_log(str(tmp_path / "after.csv"), s, ChannelNamer(s), [1, 3, 40], rows)
    proj, ecu = ovt.import_file(LQ)
    HW.add(proj, ecu.id, "pd_injectors", "BHW injectors, complete injectors",
           {"preset": "bhw", "complete": True}, since="2026-10-10")
    with open(log, "rb") as fh:
        data = fh.read()
    proj.add_log("old.csv", b"t,rpm\n0,900\n", set_id=ecu.id).added = "2026-10-01T10:00:00"
    proj.add_log("after.csv", data, set_id=ecu.id).added = "2026-10-12T10:00:00"
    path = str(tmp_path / "lq.ovt")
    proj.save(path)
    h = _open(env, path)
    hw = call(env, "injector_hardware", handle=h)
    assert hw["fitted"]["recorded"]["parts"]["preset"] == "bhw"
    assert hw["fitted"]["this_version_set_up_for"] is None and "not set up" in hw["fitted"]["note"]
    r = call(env, "injector_learn_report", handle=h, since_change=True)
    assert r["logs_used"]["after_logs"] and r["logs_used"]["fitted"] == "2026-10-10"
    # the file from before the change is no data log: left out, not fatal
    assert [x["log"].endswith("old.csv") for x in r["logs_used"]["left_out"]] == [True]
    assert r["breakpoints_corrected"] > 0

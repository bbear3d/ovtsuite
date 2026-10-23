# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The modules' AI tools (phase 6) against reference binaries: found through
the module convention, scoped to their platform, read-only except fault-code
proposals, which go through the normal proposal path. Slow (sessions)."""

import glob
import json
import os

import pytest

from ovtsuite.ai import proposals as P
from ovtsuite.ai.tools import Context, default_registry, module_tools
from ovtsuite.ai.workspace import FileWorkspace

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")
ZF = os.path.join(ROOT, "Reference", "ZF6HP", "3.0TDI-Stock.bin")
LOG = os.path.join(ROOT, "Reference", "Logs", "ecu-lq-stock_20261010_201650.csv")


def _need(*p):
    for x in p:
        if not os.path.exists(x):
            pytest.skip("missing %s" % x)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv(P.ENV_DIR, str(tmp_path))
    return default_registry(), Context(FileWorkspace(), client="pytest")


def call(env, tool, **a):
    reg, ctx = env
    ok, r = reg.call(tool, ctx, a)
    assert ok, r
    json.dumps(r)
    return r


def test_modules_contribute_their_tools():
    by = {t.name: t.module for t in module_tools()}
    for name in ("analyse_engine_logs", "coding_states", "vss_settings", "gear_and_cruise"):
        assert by.get(name) == "edc16"
    for name in ("shift_schedules", "lockup_schedules", "tcc_characteristics", "shift_feel_maps",
                 "tcu_fault_codes", "shift_torque_report"):
        assert by.get(name) == "zf6hp"
    assert "fault_codes" in by and "output_tests" in by
    assert by.get("propose_tcu_fault_codes") == "zf6hp"
    for name in ("cluster_info", "new_k_value", "sweep_patches", "propose_sweep_patch"):
        assert by.get(name) == "vdo"
    # a module tool never shadows a base tool
    reg = default_registry()
    assert reg.get("get_map").module == ""


def test_edc16_tools_on_the_lq(env):
    _need(LQ)
    call(env, "open_file", path=LQ)
    fc = call(env, "fault_codes", handle="h1", filter="EGR")
    assert fc["total"] > 100 and fc["paths"]
    mb = call(env, "measuring_blocks", handle="h1", group=3)
    assert mb["blocks"][0]["group"] == 3 and len(mb["blocks"][0]["channels"]) == 4
    assert call(env, "output_tests", handle="h1")["tests"]
    assert call(env, "coding_states", handle="h1")["codings"]
    gc = call(env, "gear_and_cruise", handle="h1")
    ratios = gc["sets"][0]["parameters"]
    assert ratios[0]["id"] == "Gearbx_rTrans1_C" and "A2L:" not in ratios[0]["title"]
    assert call(env, "vss_settings", handle="h1")["settings"]
    # platform scoping: a 6HP tool says what it is for
    reg, ctx = env
    ok, err = reg.call("shift_schedules", ctx, {"handle": "h1"})
    assert not ok and "ZF 6HP" in err["error"]


def test_fault_code_proposal_goes_through_review(env):
    _need(LQ)
    reg, ctx = env
    call(env, "open_file", path=LQ)
    on = [p for p in call(env, "fault_codes", handle="h1", enabled_only=True)["paths"]
          if p["pcode"]]
    target = on[0]
    r = call(env, "propose_fault_codes", handle="h1", rationale="not fitted",
             disable=[target["path"]])
    prop = P.load(r["proposal"])
    t = ctx.workspace.get("h1")
    P.apply_to_session(prop, t.session, confirm_untrusted=True)
    after = {p["path"]: p for p in call(env, "fault_codes", handle="h1")["paths"]}
    assert after[target["path"]]["enabled"] is False
    t.session.undo()


def test_engine_log_analysis(env):
    _need(LQ, LOG)
    call(env, "open_file", path=LQ)
    r = call(env, "analyse_engine_logs", handle="h1", paths=[LOG])
    assert r["findings"] and r["worst"]
    for ch in r["changes"]:
        assert ch["map"] and ch["cells"]


def test_zf6hp_tools(env):
    _need(ZF)
    call(env, "open_file", path=ZF)
    sk = call(env, "shift_schedules", handle="h1", coding=1)["schedules"]
    assert sk and all(s["coding"] == 1 for s in sk)
    lk = call(env, "lockup_schedules", handle="h1")
    assert lk["schedules"] and lk["control"]
    got = call(env, "shift_feel_maps", handle="h1", shift="3>2")
    sf = got["maps"]
    assert sf and all(m["shift"] == "3>2" for m in sf)
    # the target slip time is listed first, as the thing to change for
    # quicker or softer shifts; the pressures are the means
    targets = [m for m in sf if m["role"] == "target"]
    assert sf[:len(targets)] == targets
    assert {m["meaning"] for m in targets} == {"target slip time 3>2 downshift",
                                              "target slip time 3>2 downshift, Sport"}
    assert all(m["role"] == "pressure" for m in sf[len(targets):])
    assert "TARGET SLIP TIME" in got["note"]
    only = call(env, "shift_feel_maps", handle="h1", clutch="slip time")["maps"]
    assert only and all(m["role"] == "target" for m in only)
    codes = call(env, "tcu_fault_codes", handle="h1", limit=5)
    assert codes["tables"] >= 1 and codes["codes"]
    reg, ctx = env
    ok, err = reg.call("coding_states", ctx, {"handle": "h1"})
    assert not ok and "EDC16" in err["error"]
    # the schedule ids work with the base tools
    g = call(env, "get_map", handle="h1", map=sk[0]["id"], comparison=False)
    assert g["shape"] == sk[0]["shape"]


TFSI = os.path.join(ROOT, "Reference", "ZF6HP", "3.0TFSI-Stock.bin")


def test_tcc_characteristics_span_every_wk_map(env):
    """Bear: the assistant changed one TCC map pair for a lock-up change. A
    gear's lock-up characteristics are picked per shift program by
    WT_KWK_ZTAB and sit in several WK maps; the tool lists them all."""
    _need(TFSI)
    call(env, "open_file", path=TFSI)
    got = call(env, "tcc_characteristics", handle="h1", gear=3)
    (g3,) = got["by_gear"]
    chars = g3["characteristics"]
    assert {c["event"] for c in chars} == {"3>4", "3>2"}       # only gear 3's own events
    assert len({c["wk_map"] for c in chars}) > 2                # spread over the maps
    for c in chars:
        grp = (c["characteristic"] - 1) % 10
        assert c["wk_map"] == (c["characteristic"] - 1) // 10 + 1
        assert c["columns"][0] == grp * len(c["columns"]) and c["map_id"]
    lk = call(env, "lockup_schedules", handle="h1")
    assert "tcc_characteristics" in lk["note"]
    assert lk["schedules"][0]["title"].endswith("characteristics 1 to 10")


VDO = os.path.join(ROOT, "Reference", "VDO", "Samples", "VWK501-FIS-Diesel-A4-UK-Base.bin")


def test_zf6hp_fault_code_switch_is_a_checked_byte_proposal(env):
    _need(ZF)
    reg, ctx = env
    call(env, "open_file", path=ZF)
    code = call(env, "tcu_fault_codes", handle="h1", enabled_only=True, limit=1)["codes"][0]
    r = call(env, "propose_tcu_fault_codes", handle="h1", rationale="test",
             disable=[code["code"]], coding_variant=code["coding_variant"])
    prop = P.load(r["proposal"])
    (b,) = prop["bytes"]
    # the factory's unused-class form: the class's settings (bytes 1-6)
    # cleared, its priority kept
    assert b["new"] == "00" * 6 and b["old"] != "00" * 6
    s = ctx.workspace.get("h1").session
    # a stale proposal never overwrites newer work
    s.edit_bytes(int(b["addr"]), bytes([0x55]))
    res = P.apply_to_session(prop, s)
    assert not res.applied and "changed since" in res.skipped[0][1]
    s.undo()
    res = P.apply_to_session(prop, s)
    assert res.applied == ["bytes@0x%X" % b["addr"]]
    a = int(b["addr"])
    assert bytes(s.binfile.data[a:a + 6]) == bytes(6)
    s.undo()


def test_vdo_tools_withhold_identity_and_propose_fitting_patches(env):
    _need(VDO)
    reg, ctx = env
    call(env, "open_file", path=VDO)
    info = call(env, "cluster_info", handle="h1")
    vals = {r["field"]: r["value"] for r in info["identity"]}
    assert vals.get("VIN") == "[withheld]" and info["k_value"]
    fi = {r["field"]: r["value"] for r in call(env, "file_information", handle="h1")["rows"]}
    assert fi.get("VIN") == "[withheld]" and fi.get("Immobilizer ID") == "[withheld]"
    ok, err = reg.call("get_map", ctx, {"handle": "h1", "map": "Immobilizer ID"})
    assert not ok and "not shared" in err["error"]
    ok, err = reg.call("propose_change", ctx, {"handle": "h1", "title": "t", "rationale": "r",
                                               "changes": [{"map": "Immobilizer ID", "add": 1}]})
    assert not ok and "immobiliser" in err["error"]
    k = call(env, "new_k_value", handle="h1", old_tyre="205/55R16", new_tyre="225/45R17")
    assert k["new_k_value"] < k["k_value"] and k["k_value_id"]
    sp = call(env, "sweep_patches", handle="h1")
    fit = [p for p in sp["patches"] if p["fits"]]
    assert fit and all(p["rom_version"] == sp["cluster_rom"] for p in fit)
    r = call(env, "propose_sweep_patch", handle="h1", name=fit[0]["name"])
    prop = P.load(r["proposal"])
    assert prop["bytes"] and "Licence" in prop["rationale"]
    s = ctx.workspace.get("h1").session
    assert P.apply_to_session(prop, s).applied
    s.undo()


def test_file_information_for_platforms_without_tools(env):
    hp8 = sorted(glob.glob(os.path.join(ROOT, "Reference", "ZF8HP", "OEM", "*.bin")))
    if not hp8:
        pytest.skip("no 8HP reference")
    call(env, "open_file", path=hp8[0])
    rows = {r["field"]: r["value"] for r in call(env, "file_information", handle="h1")["rows"]}
    assert rows["Platform"].startswith("ZF 8HP") and rows.get("Part number")

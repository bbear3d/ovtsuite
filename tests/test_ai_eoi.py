# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The assistant's end-of-injection tools (ovtsuite.ai.eoi_tools): the same
numbers as Tools > End of injection, and changes that pass check_changes."""
import json
import os

import pytest

from ovtsuite.ai import proposals as P
from ovtsuite.ai.tools import Context, default_registry
from ovtsuite.ai.workspace import FileWorkspace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LQ = os.path.join(ROOT, "Reference", "Binaries", "03G906016LQ.bin")
GQ = os.path.join(ROOT, "Reference", "Binaries", "EDC15", "038906019GQ.bin")
VE = os.path.join(ROOT, "Reference", "Binaries", "EDC15", "038906012HA.bin")


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


def test_registered(env):
    reg, _ = env
    assert reg.get("end_of_injection") and reg.get("end_of_injection").read_only
    assert reg.get("end_of_injection_changes").read_only     # files nothing


def test_lq_point_matches_the_window(env):
    h = _open(env, LQ)
    r = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20)
    p = r["point"]
    assert p["start_btdc"] == pytest.approx(3.96, abs=0.02)
    assert p["duration_table_number"] == pytest.approx(3.84, abs=0.01)
    assert [m["table"] for m in p["duration_tables"]] == [3, 4]
    assert p["eoi"] == pytest.approx(5.87, abs=0.02)
    g = call(env, "end_of_injection", handle=h, values="all")["grid"]
    assert len(g["eoi"]) == len(g["rpm"]) and len(g["eoi"][0]) == len(g["quantity"])
    assert "start_btdc" in g and "duration" in g


def test_lq_changes_pass_check_changes(env):
    h = _open(env, LQ)
    r = call(env, "end_of_injection_changes", handle=h, target_eoi=-2.0,
             rpm_min=1500, rpm_max=3000, quantity_min=10, quantity_max=40)
    assert r["changes"]
    chk = call(env, "check_changes", handle=h, changes=r["changes"])
    assert chk["ok"], chk["problems"]


def test_gq_manual_1(env):
    h = _open(env, GQ)
    r = call(env, "end_of_injection", handle=h, dataset="Manual 1", rpm=2000, quantity=20)
    assert r["point"]["eoi"] == pytest.approx(4.78, abs=0.02)
    assert "Manual 1" in r["datasets"]


def test_ve_pump_is_a_plain_error(env):
    h = _open(env, VE)
    reg, ctx = env
    ok, r = reg.call("end_of_injection", ctx, {"handle": h})
    assert not ok and "no end-of-injection model" in r["error"]


def test_lq_dynamic_set(env):
    h = _open(env, LQ)
    base = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20)
    assert {s["key"] for s in base["soi_sets"]} >= {"base", "dyn"}
    dyn = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20, soi_set="dyn")
    assert dyn["soi_set"] == "dyn"
    assert dyn["point"]["start_btdc"] == pytest.approx(11.06, abs=0.02)
    assert dyn["point"]["eoi"] < base["point"]["eoi"] - 5
    reg, ctx = env
    ok, r = reg.call("end_of_injection", ctx, {"handle": h, "soi_set": "nope"})
    assert not ok and "dyn" in r["error"]


def test_lq_start_set(env):
    h = _open(env, LQ)
    r = call(env, "end_of_injection", handle=h, soi_set="start", start_quantity=40)
    assert "coolant" in r["grid"] and "note" in r
    assert set(r["conditions"]) == {"air_pressure", "start_quantity"}


def test_lq_fuel_temperature(env):
    h = _open(env, LQ)
    r = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20, fuel_temperature=20)
    assert r["point"]["duration_quantity"] == pytest.approx(18.22, abs=0.01)
    assert r["conditions"]["fuel_temperature"] == 20
    r = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20)
    assert r["conditions"]["fuel_temperature"] == "reference"


def test_units_microseconds_for_a_point_and_the_grid(env):
    h = _open(env, LQ)
    from ovtsuite import eoi
    deg = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20)["point"]
    us = call(env, "end_of_injection", handle=h, rpm=2000, quantity=20,
              units="microseconds")
    assert us["units"] == "microseconds"
    for k in ("start_btdc", "duration", "eoi"):
        assert us["point"][k] == pytest.approx(eoi.deg_to_us(deg[k], 2000.0), abs=0.6)
        assert us["point"]["degrees"][k] == pytest.approx(deg[k])
    g = call(env, "end_of_injection", handle=h, values="all",
             units="microseconds")["grid"]
    gd = call(env, "end_of_injection", handle=h, values="all")["grid"]
    r, q = len(g["rpm"]) // 2, len(g["quantity"]) // 2
    assert g["eoi"][r][q] == pytest.approx(
        eoi.deg_to_us(gd["eoi"][r][q], float(g["rpm"][r])), abs=1.0)


def test_units_microseconds_target_is_per_rpm(env):
    h = _open(env, LQ)
    ok, r = env[0].call("end_of_injection_changes", env[1],
                        {"handle": h, "target_eoi": 500, "units": "microseconds"})
    assert ok, r
    assert r["units"] == "microseconds"
    ok, bad = env[0].call("end_of_injection", env[1], {"handle": h, "units": "furlongs"})
    assert not ok

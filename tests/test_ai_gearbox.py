# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The assistant's full-load shift-point tool (ovtsuite.ai.gearbox_tools):
the shift editor's step on a project's gearbox, changes that pass
check_changes."""
import json
import os

import pytest

from ovtsuite.ai import proposals as P
from ovtsuite.ai.tools import Context, default_registry
from ovtsuite.ai.workspace import FileWorkspace

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TUNE = os.path.join(ROOT, "Reference", "Tunes", "2010-A6-BHW-6HP-19A.ovt")


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    os.environ[P.ENV_DIR] = str(tmp_path_factory.mktemp("ai"))
    reg = default_registry()
    ctx = Context(FileWorkspace(), client="pytest")
    yield reg, ctx
    os.environ.pop(P.ENV_DIR, None)


def call(env, name, **args):
    reg, ctx = env
    ok, r = reg.call(name, ctx, args)
    assert ok, r
    json.dumps(r)
    return r


def test_registered_read_only(env):
    reg, _ = env
    assert reg.get("fullload_shift_points").read_only


def test_full_load_points_from_the_projects_ecu(env):
    if not os.path.exists(TUNE):
        pytest.skip("Bear's project is not in Reference/Tunes")
    r = call(env, "open_file", path=TUNE, set="6HP-19A")
    h = r["opened"][0]["handle"]
    r = call(env, "fullload_shift_points", handle=h,
             schedules="Coding 1: D (economic) — adaptive grid", tyre="245/45R18")
    assert r["ecu"]["set"] == "Stock" and "kW" in r["ecu"]["curve"]
    assert r["converter_open_in_gears"] == [1]
    assert [x["line"] for x in r["targets"]] == ["1>2", "2>3", "3>4", "4>5", "5>6"]
    assert r["changes"]
    ok = call(env, "check_changes", handle=h, changes=r["changes"])
    assert not ok.get("errors"), ok
    # a gearbox file on its own has no ECU to work from
    reg, ctx = env
    okc, err = reg.call("fullload_shift_points", ctx, {"handle": "nope"})
    assert not okc


def test_part_load_strategy_and_replay(env):
    if not os.path.exists(TUNE):
        pytest.skip("Bear's project is not in Reference/Tunes")
    reg, _ = env
    assert reg.get("partload_shift_strategy").read_only
    assert reg.get("replay_shift_lines").read_only
    r = call(env, "open_file", path=TUNE, set="6HP-19A")
    h = r["opened"][0]["handle"]
    r = call(env, "partload_shift_strategy", handle=h,
             schedules="Coding 1: D (economic) — adaptive grid")
    assert r["strategies"] == {"D": "economy", "S": "sport", "other": "leave"}
    assert 80 <= r["full_torque_pedal"] <= 95
    assert r["changes"]
    assert any(f["check"] == "converter" for f in r["findings"])
    ok = call(env, "check_changes", handle=h, changes=r["changes"])
    assert not ok.get("errors"), ok
    rp = call(env, "replay_shift_lines", handle=h, changes=r["changes"],
              schedules="Coding 1: D (economic) — adaptive grid")
    assert rp["hours"] > 1.0 and rp["cells_applied"] > 0
    assert rp["before"]["in_logged_gear_pct"] > 80
    assert rp["after"]["shifts_per_hour"] != rp["before"]["shifts_per_hour"]

# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""AI tools against real binaries: identify, search, maps, diff, proposals,
and accepting a proposal into a session. Slow (whole-image sessions)."""

import json
import os

import pytest

from ovtsuite.ai import proposals as P
from ovtsuite.ai.tools import Context, default_registry
from ovtsuite.ai.workspace import FileWorkspace

pytestmark = pytest.mark.slow

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "Reference", "Binaries", "038906016Q.Bin")
TUNED = os.path.join(ROOT, "Reference", "Binaries", "038906016Q-Stg2-MPPS Read.Bin")
SMOKE = "FlMng_qPresSmoke_MAP"

if not (os.path.isfile(BIN) and os.path.isfile(TUNED)):
    pytest.skip("reference binaries not present", allow_module_level=True)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    os.environ[P.ENV_DIR] = str(tmp_path_factory.mktemp("ai"))
    reg = default_registry()
    ctx = Context(FileWorkspace(), client="pytest")
    ok, r = reg.call("open_file", ctx, {"path": BIN, "compare_with": TUNED})
    assert ok, r
    yield reg, ctx
    os.environ.pop(P.ENV_DIR, None)


def call(env, name, **args):
    reg, ctx = env
    ok, r = reg.call(name, ctx, args)
    assert ok, r
    json.dumps(r)                         # every result is JSON-serialisable
    return r


def test_identify(env):
    r = call(env, "identify", handle="h1")
    assert r["platform"] == "edc16" and r["support"] == "full"
    assert r["parameters"] > 1000
    assert r["checksum"] is not None


def test_find_and_get_map(env):
    hits = call(env, "find_maps", handle="h1", query="smoke limiter")["results"]
    assert any(h["id"] == SMOKE for h in hits)
    m = call(env, "get_map", handle="h1", map=SMOKE)
    assert len(m["values"]) == m["shape"][0]
    assert all(len(r) == m["shape"][1] for r in m["values"])
    assert m["placement"]["grade"]
    assert "comparison" in m and m["allowed_range"][0] < m["allowed_range"][1]


def test_unknown_map_is_an_error_not_a_guess(env):
    reg, ctx = env
    ok, r = reg.call("get_map", ctx, {"handle": "h1", "map": "NoSuch_MAP"})
    assert not ok and "find_maps" in r["error"]


def test_diff_lists_changed_maps(env):
    r = call(env, "diff", handle="h1", limit=500)
    assert r["changed"] > 0
    ids = [m["id"] for m in r["maps"]]
    first = call(env, "get_map", handle="h1", map=ids[0])
    assert first["comparison"]["changed_cells"] > 0
    listed = call(env, "list_maps", handle="h1", changed_only=True, limit=500)
    assert listed["total"] >= 1


def test_out_of_range_is_refused_and_check_changes_says_why(env):
    reg, ctx = env
    e = ctx.workspace.get("h1").index.resolve(SMOKE)
    lo, hi = e.value_range
    bad = [{"map": SMOKE, "cells": [{"row": 0, "col": 0, "value": hi * 10}]}]
    r = call(env, "check_changes", handle="h1", changes=bad)
    assert r["ok"] is False and r["problems"][0]["code"] == "clamped"
    ok, err = reg.call("propose_change", ctx, {"handle": "h1", "title": "t",
                                               "rationale": "r", "changes": bad})
    assert not ok and "not filed" in err["error"]
    assert call(env, "list_proposals")["proposals"] == []
    good = [{"map": SMOKE, "add": 1.0}]
    r = call(env, "check_changes", handle="h1", changes=good)
    assert r["ok"] is True and r["maps"][0]["cells_changed"] == e.n_cells


def test_proposal_composes_and_applies(env):
    reg, ctx = env
    t = ctx.workspace.get("h1")
    e = t.index.resolve(SMOKE)
    before = [list(r) for r in e.values()]
    r = call(env, "propose_change", handle="h1", title="test", rationale="why",
             changes=[{"map": SMOKE, "add": 1.0},
                      {"map": SMOKE, "cells": [{"row": 0, "col": 0, "value": before[0][0] + 5}]}])
    (item,) = r["summary"]                       # two changes, one map
    assert item["changed_cells"] == e.n_cells
    # nothing written to the binary by proposing
    assert e.values() == before
    pid = r["proposal"]
    assert pid in [p["id"] for p in call(env, "list_proposals", handle="h1")["proposals"]]

    prop = P.load(pid)
    s = t.session
    res = P.apply_to_session(prop, s, confirm_untrusted=True)
    assert res.applied == [SMOKE] and res.record_id
    after = e.values()
    assert after[0][0] == pytest.approx(e.clamp(before[0][0] + 5), abs=1e-6)
    if e.rows * e.cols > 1:
        r1, c1 = (0, 1) if e.cols > 1 else (1, 0)
        assert after[r1][c1] == pytest.approx(e.clamp(before[r1][c1] + 1.0), abs=1e-6)
    assert any(l.startswith("PATCH ") for l in s.change_log)
    s.undo()                                      # one undo step
    assert e.values() == before

    call(env, "withdraw_proposal", id=pid)
    assert pid not in [p["id"] for p in call(env, "list_proposals")["proposals"]]


def test_plan_steps_build_on_each_other(env):
    reg, ctx = env
    t = ctx.workspace.get("h1")
    e = t.index.resolve(SMOKE)
    other = next(x for x in t.session.entities if x.kind == "map" and x.name
                 and x.name != SMOKE and x.n_cells <= 64 and x.source in ("anchored", "verified"))
    before = e.values()
    r = call(env, "propose_plan", handle="h1", title="Stage 1", goal="more power",
             steps=[{"title": "smoke", "rationale": "a", "changes": [{"map": SMOKE, "add": 1}]},
                    {"title": "other", "rationale": "b", "changes": [{"map": other.name, "add": 1}]},
                    {"title": "smoke again", "rationale": "c", "changes": [{"map": SMOKE, "add": 1}]}])
    assert len(r["steps"]) == 3
    props = [P.load(s["proposal"]) for s in r["steps"]]
    assert {p["plan"]["id"] for p in props} == {r["plan"]}
    assert [p["plan"]["step"] for p in props] == [1, 2, 3]
    assert props[0]["title"].startswith("Stage 1 - 1/3")
    # step 3 starts from step 1's result
    v3 = props[2]["patch"]["entries"][0]["values"]
    assert v3[0][0] == pytest.approx(e.clamp(e.clamp(before[0][0] + 1) + 1), abs=1e-6)
    for p in props:
        call(env, "withdraw_proposal", id=p["id"])


def test_project_set_compares_with_as_read_original(env, tmp_path):
    from ovtsuite.project.importers import import_file
    proj, s = import_file(BIN)
    with open(TUNED, "rb") as fh:
        tuned = fh.read()
    main = s.members[0]
    proj.commit(s.id, None, {main.id: tuned}, label="stage 2")
    path = proj.save(str(tmp_path / "car.ovt"))
    reg, ctx = env
    ok, r = reg.call("open_file", ctx, {"path": path})
    assert ok, r
    h = r["opened"][0]["handle"]
    hist = call(env, "history", handle=h)["checkpoints"]
    assert hist[0]["label"] == "stage 2" and len(hist) >= 2
    d = call(env, "diff", handle=h, limit=5)
    assert d["changed"] > 0 and d["comparison"].startswith("as-read original")
    info = call(env, "project_info", handle=h)
    assert info["sets"][0]["branches"]
    p = call(env, "propose_change", handle=h, title="t", rationale="r",
             changes=[{"map": SMOKE, "add": 1.0}])
    tgt = P.load(p["proposal"])["target"]
    assert tgt["kind"] == "project-set" and tgt["project_uid"] == proj.uid
    call(env, "withdraw_proposal", id=p["proposal"])

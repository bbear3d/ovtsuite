# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Part-load shift strategy, the pedal-axis move and the log replay
(ovtsuite.gearbox_edit.partload / .replay) -- synthetic, no Qt; the slow tests
run on Bear's project."""
import os

import pytest

from ovtsuite.gearbox_edit import Edit, Line, Schedule, ScheduleSet, Scope
from ovtsuite.gearbox_edit import partload as PL
from ovtsuite.gearbox_edit import replay as RP

PEDAL = (0.0, 11.8, 29.9, 43.3, 55.5, 66.9, 80.7, 87.8, 94.5, 97.3, 100.0, 100.4)
R = (2.34, 1.521)


class FakeAxis:
    def __init__(self, vals):
        self.vals = list(vals)
        self.eq = None
        self.is_float = True


class FakeEntity:
    is_float = False

    def __init__(self, rows, cols, z, name="m"):
        self.rows, self.cols, self.z, self.name, self.label = rows, cols, list(z), name, name
        self.y = FakeAxis(PEDAL)

    def cell(self, r, c):
        return self.z[r * self.cols + c]

    def y_values(self):
        return list(self.y.vals)

    def clamp(self, v):
        return float(round(max(0, min(65535, v))))

    value_range = (0, 65535)


LINES = [Line(0, "2>3", "up", R[0], R[1], pair=1, gear_left=2, gear_entered=3),
         Line(1, "3>2", "down", R[1], R[0], pair=0, gear_left=3, gear_entered=2)]


def sched(key, n, group, role=""):
    up = [round(v / R[0]) for v in (2177, 2177, 2177, 2177, 2179, 2225, 2343, 2594, 2975,
                                    3533, 3533, 3767)]
    dn = [round(v / R[1]) for v in (850, 850, 850, 850, 905, 1018, 1202, 1478, 1594, 1926,
                                    2178, 2449)]
    z = [v for row in zip(up, dn) for v in row]
    return Schedule(key=key, entity=FakeEntity(len(PEDAL), 2, z, key), number=n,
                    group=group, role=role, pedal=PEDAL)


def make():
    return ScheduleSet([sched("SKL 11.0", 11, "D (economic) — adaptive grid"),
                        sched("SKL 12.0", 12, "S (sport) — adaptive grid"),
                        sched("SKL 27.0", 27, "M (manual)"),
                        sched("SKL 2.0", 2, "Special programs", "program 36")], LINES)


def col(grid, s, i, c):
    return [grid[i][r * s.cols + c] for r in range(s.rows)]


ANCHOR = {0: 4200.0 / R[0]}


def test_presets_order_economy_low_and_late_sport_high_and_early():
    e, s = PL.ECONOMY, PL.SPORT
    assert e.floor < PL.BALANCED.floor < s.floor
    assert e.rise(50) < s.rise(50)
    assert e.rise(100) == s.rise(100) == 1.0
    assert e.rise(60, full_torque=55.5) == 1.0


def test_d_economy_s_sport_m_left_alone():
    sset = make()
    pick = PL.assign_by_mode({"D": PL.ECONOMY, "S": PL.SPORT, "M": None})
    op = PL.PartLoadTarget(Scope([0, 1, 2, 3], [0]), pick, anchors=ANCHOR)
    res = Edit(sset, [op]).compute()
    d, s_ = (col(res.grid, sset.schedules[i], i, 0) for i in (0, 1))
    m = col(res.grid, sset.schedules[2], 2, 0)
    assert m == sset.schedules[2].values()[0::2]            # M untouched
    assert {c.key[0] for c in res.changes} == {0, 1}        # "other" left alone by default
    k = R[1] / R[0]
    # light pedal: the economy floor, post-shift speed 1250 in 3rd
    assert d[0] * R[0] * k == pytest.approx(1250, abs=2)
    assert s_[0] * R[0] * k == pytest.approx(1850, abs=2)
    assert all(a <= b + 0.5 for a, b in zip(d[:10], s_[:10]))
    # full-load rows are the full-load step's: untouched
    assert d[10:] == sset.schedules[0].values()[20::2]
    # rising with pedal
    assert all(a <= b + 0.5 for a, b in zip(d[:10], d[1:10]))


def test_downshift_sits_below_the_upshift_by_the_gap():
    sset = make()
    op = PL.PartLoadTarget(Scope([0], [0]), lambda s: PL.ECONOMY, anchors=ANCHOR)
    res = Edit(sset, [op]).compute()
    s = sset.schedules[0]
    up, dn = col(res.grid, s, 0, 0), col(res.grid, s, 0, 1)
    for r in range(10):
        post_up = up[r] * R[1]               # rpm in 3rd just after the upshift
        assert dn[r] * R[1] <= post_up * 0.90 + 1.5


def test_full_torque_pedal_brings_the_full_load_point_down_to_that_row():
    sset = make()
    op = PL.PartLoadTarget(Scope([0], [0]), lambda s: PL.ECONOMY, anchors=ANCHOR,
                           full_torque=87.8, full_load_rows=True)
    res = Edit(sset, [op]).compute()
    up = col(res.grid, sset.schedules[0], 0, 0)
    for r in (7, 8, 9, 10):
        assert up[r] == pytest.approx(ANCHOR[0], abs=0.6)


def test_an_anchor_never_takes_part_load_above_the_full_load_row():
    """The full-load step's planned point, with that step not applied (or
    skipping the line): part load stops at the schedule's own full-load row,
    so the line never falls going into full load."""
    sset = make()
    s = sset.schedules[0]
    own = col(Edit(sset, []).compute().grid, s, 0, 0)[10]      # the 100 % row
    high = {0: own + 300.0}
    res = Edit(sset, [PL.PartLoadTarget(Scope([0], [0]), lambda _s: PL.ECONOMY,
                                        anchors=high, full_torque=87.8)]).compute()
    up = col(res.grid, s, 0, 0)
    assert max(up[:10]) <= up[10] + 0.6


def test_fraction_moves_part_of_the_way():
    sset = make()
    full = Edit(sset, [PL.PartLoadTarget(Scope([0], [0]), lambda s: PL.SPORT,
                                         anchors=ANCHOR)]).compute()
    half = Edit(sset, [PL.PartLoadTarget(Scope([0], [0]), lambda s: PL.SPORT, anchors=ANCHOR,
                                         fraction=0.5)]).compute()
    was = sset.schedules[0].values()[0::2]
    f, h = col(full.grid, sset.schedules[0], 0, 0), col(half.grid, sset.schedules[0], 0, 0)
    for r in range(10):
        assert h[r] == pytest.approx((was[r] + f[r]) / 2, abs=1.0)


def test_without_anchors_the_schedules_own_full_load_row_is_the_target():
    sset = make()
    res = Edit(sset, [PL.PartLoadTarget(Scope([0], [0]), lambda s: PL.SPORT,
                                        full_torque=87.8)]).compute()
    up = col(res.grid, sset.schedules[0], 0, 0)
    assert up[8] == pytest.approx(round(3533 / R[0]), abs=0.6)


def test_remap_pedal_axis_moves_breakpoints_not_values():
    assert PL.remap_pedal(30.0, 50.0, 97.3, 85.0) == 30.0
    assert PL.remap_pedal(97.3, 50.0, 97.3, 85.0) == pytest.approx(85.0)
    assert PL.remap_pedal(100.0, 50.0, 97.3, 85.0) == 100.0
    sset = make()
    op = PL.RemapPedalAxis(Scope([0, 1], [0]), 85.0, keep_below=50.0)
    res = Edit(sset, [op]).compute()
    assert not res.changes and len(res.axes) == 2
    new = res.pedal_of(sset, 0)
    assert new[:4] == PEDAL[:4] and new[-2:] == PEDAL[-2:]
    assert new[9] == pytest.approx(85.0)
    assert all(a < b for a, b in zip(new, new[1:]))
    assert "pedal axes of 2 maps" in res.summary(sset)


def test_replay_counts_fuel_shifts_and_shortfall():
    sset = make()
    grid = {i: s.values() for i, s in enumerate(sset.schedules)}
    corners = RP.program_corners(sset, [], [0])
    up, dn = RP.line_source(sset, grid, lambda i: sset.schedules[i].pedal, corners)
    ratios = {2: R[0], 3: R[1]}
    # accelerate in 2nd at 30 % pedal past the 2>3 line, then cruise
    out = [700 + 20 * k for k in range(60)] + [1900] * 60
    smp = [RP.Sample(1.0, o / 1000 * 60, 29.9, 2 if k < 50 else 3, o, 20.0)
           for k, o in enumerate(out)]
    m = RP.replay(smp, ratios, up, dn, lambda t, n: t * n / 9549.3 * 0.25, lambda n: 300.0)
    assert m.shifts_h > 0 and m.l100 > 0 and m.short_s == 0
    m2 = RP.replay(smp, ratios, up, dn, lambda t, n: 1.0, lambda n: 10.0)
    assert m2.short_s > 0
    # the cruise program reads its own schedule ("program 36")
    c = corners(RP.Sample(1, 50, 20, 3, 1500, 5, program=36))
    assert c == [(3, 1.0)]
    assert corners(RP.Sample(1, 50, 20, 3, 1500, 5, program=99)) == [(0, 1.0)]

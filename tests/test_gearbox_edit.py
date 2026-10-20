# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The multi-schedule editing core (ovtsuite.gearbox_edit), on synthetic
schedules -- no reference binaries, no Qt."""
import pytest

from ovtsuite.gearbox_edit import (BandRemap, CopyFrom, Edit, GapCheck, Line,
                                   MonotonicCheck, Offset, RpmLimitCheck, Scale,
                                   Schedule, ScheduleSet, Scope, SetCurve,
                                   SharedCheck, SlidePedal, Smooth,
                                   effective_line, parse_range)

PEDAL = (0.0, 20.0, 40.0, 60.0, 80.0, 90.0, 100.0, 100.4)


class FakeEntity:
    """Just what the core and autotune.Plan touch."""
    is_float = False

    def __init__(self, rows, cols, z, name="m"):
        self.rows, self.cols, self.z, self.name, self.label = rows, cols, list(z), name, name

    def cell(self, r, c):
        return self.z[r * self.cols + c]

    def clamp(self, v):
        return float(round(max(0, min(65535, v))))

    value_range = (0, 65535)


# lines: 0 = 4>5 (up), 1 = 5>4 (down) -- ratios of 4th / 5th gear
LINES = [Line(0, "4>5", "up", 1.143, 0.867, pair=1, gear_left=4, gear_entered=5),
         Line(1, "5>4", "down", 0.867, 1.143, pair=0, gear_left=5, gear_entered=4)]


def sched(key, number, bank, up, down, states=()):
    z = [v for pair in zip(up, down) for v in pair]
    return Schedule(key=key, entity=FakeEntity(len(PEDAL), 2, z, key), number=number,
                    bank=bank, states=tuple(states), pedal=PEDAL)


def make():
    a = sched("SKL 11.0", 11, 0, [2000, 2000, 2100, 2300, 2500, 2700, 2900, 3000],
              [1500, 1500, 1550, 1700, 1900, 2100, 2300, 2400], states=(11,))
    b = sched("SKL 16.0", 16, 0, [2100, 2150, 2200, 2400, 2600, 2800, 3000, 3100],
              [1600, 1620, 1650, 1750, 1950, 2150, 2350, 2450], states=(6, 16))
    c = sched("SKL 11.1", 11, 1, [2000] * 8, [1500] * 8)
    return ScheduleSet([a, b, c], LINES)


def col(res, i, c, sset):
    s = sset.schedules[i]
    return [res.grid[i][r * s.cols + c] for r in range(s.rows)]


def test_offset_in_engine_rpm_converts_with_the_lines_ratio():
    sset = make()
    res = Edit(sset, [Offset(Scope([0], [1]), 86.7, "engine")]).compute()
    assert col(res, 0, 1, sset)[0] == 1500 + 100          # 86.7 / 0.867


def test_scope_pedal_range_and_taper():
    sset = make()
    sc = Scope([0], [1], pedal_max=99.9, taper_start=80, taper_end=100)
    res = Edit(sset, [Offset(sc, 100, "output")]).compute()
    got = col(res, 0, 1, sset)
    assert got[:5] == [1600, 1600, 1650, 1800, 2000]       # full up to 80 %
    assert got[5] == 2100 + 50                             # half way at 90 %
    assert got[6:] == [2300, 2400]                         # 100 / 100.4 untouched


def test_slide_reads_the_curve_further_up_the_pedal_axis():
    sset = make()
    res = Edit(sset, [SlidePedal(Scope([0], [1], direction="raise"), 20)]).compute()
    got = col(res, 0, 1, sset)
    assert got[0] == 1500 and got[1] == 1550 and got[2] == 1700


def test_raise_only_never_lowers():
    sset = make()
    res = Edit(sset, [Offset(Scope([0], [1], direction="raise"), -100, "output")]).compute()
    assert res.changes == []


def test_band_remap_keeps_order_and_spacing():
    sset = make()
    res = Edit(sset, [BandRemap(Scope([0, 1], [0]), 2000, 2400, "output")]).compute()
    a, b = col(res, 0, 0, sset), col(res, 1, 0, sset)
    assert all(x == 2000 for x in a) and all(y == 2400 for y in b)


def test_set_curve_and_copy_and_smooth():
    sset = make()
    res = Edit(sset, [SetCurve(Scope([2], [0]), [(0, 1800), (100, 2800)], "output")]).compute()
    assert col(res, 2, 0, sset)[2] == 2200
    res = Edit(sset, [CopyFrom(Scope([2], [0]), source=0, fraction=0.5)]).compute()
    assert col(res, 2, 0, sset)[3] == (2000 + 2300) / 2
    res = Edit(sset, [Smooth(Scope([0], [0]), 1.0)]).compute()
    assert col(res, 0, 0, sset)[2] == round((2000 + 2300) / 2)


def test_gap_check_own_gap_and_bounded_fix():
    sset = make()
    sc = Scope([0], [1], direction="raise")
    # 5>4 raised to within 50 of 4>5 at 0 %: own gap 500, min_gap 60 -> needs 60
    edit = Edit(sset, [Offset(sc, 450, "output")], [GapCheck(60)])
    res = edit.compute()
    assert any(f.code == "gap" for f in res.findings)
    edit = Edit(sset, [Offset(sc, 450, "output")], [GapCheck(60, autofix=True)])
    res = edit.compute()
    assert col(res, 0, 1, sset)[0] == 2000 - 60
    assert all(f.fixed for f in res.findings if f.code == "gap")


def test_gap_check_does_not_report_what_the_edit_did_not_worsen():
    sset = make()
    res = Edit(sset, [Offset(Scope([0], [1]), -50, "output")], [GapCheck(1000)]).compute()
    assert not [f for f in res.findings if f.code == "gap"]


def test_monotonic_pull_back_and_raise():
    sset = make()
    sc = Scope([0], [1], pedal_min=20, pedal_max=20)
    res = Edit(sset, [Offset(sc, 300, "output")],
               [MonotonicCheck(autofix=True, mode="pull_back")]).compute()
    got = col(res, 0, 1, sset)
    assert got[1] == 1550            # pulled back to the next row, not below 1500
    res = Edit(sset, [Offset(sc, 300, "output")],
               [MonotonicCheck(autofix=True, mode="raise")]).compute()
    got = col(res, 0, 1, sset)
    assert got[1] == 1800 and got[2] == 1800 and got[3] == 1800 and got[4] == 1900


def test_rpm_limit_and_shared_note():
    sset = make()
    res = Edit(sset, [Offset(Scope([1], [0]), -900, "output")],
               [RpmLimitCheck(floor=1200), SharedCheck()]).compute()
    codes = {f.code for f in res.findings}
    assert "rpm" in codes and "shared" in codes


def test_uniform_edit_moves_the_blend_by_exactly_that_amount():
    """Whatever the weights, an identical offset on every corner moves the
    effective line by the same offset -- the editor relies on this."""
    sset = make()
    corners = [(0, 0.58), (1, 0.43)]
    before = effective_line(sset, {i: s.values() for i, s in enumerate(sset.schedules)},
                            corners, 1, [30, 50, 70])
    res = Edit(sset, [Offset(Scope([0, 1], [1]), 100, "output")]).compute()
    after = effective_line(sset, res.grid, corners, 1, [30, 50, 70])
    assert [round(b - a, 6) for a, b in zip(before, after)] == [100.0] * 3


def test_parse_range():
    sset = make()
    assert parse_range("11", sset) == [0, 2]
    assert parse_range("11.1", sset) == [2]
    assert parse_range("10-16", sset) == [0, 1, 2]
    with pytest.raises(ValueError):
        parse_range("eleven", sset)


def test_plan_carries_every_change_and_no_blocking_problem():
    sset = make()
    edit = Edit(sset, [Scale(Scope([0, 1], [0]), 0.9)], [GapCheck(60)])
    plan = edit.to_plan(None, "test")
    assert len(plan.effective_writes) == edit.compute().cells
    assert plan.ok and not plan.errors


# ------------------------------------------------- gap, off, identical groups
def test_set_gap_moves_the_upper_line_from_the_lower():
    from ovtsuite.gearbox_edit import SetGap
    sset = make()
    res = Edit(sset, [SetGap(Scope([0], [1], pedal_min=20, pedal_max=60), 300, "output",
                             "low")]).compute()
    up, dn = col(res, 0, 0, sset), col(res, 0, 1, sset)
    assert [u - d for u, d in zip(up, dn)][1:4] == [300, 300, 300]
    assert dn == [1500, 1500, 1550, 1700, 1900, 2100, 2300, 2400]   # lower kept
    assert up[0] == 2000 and up[4] == 2500                          # outside the range


def test_set_gap_about_the_middle_and_high_anchor():
    from ovtsuite.gearbox_edit import SetGap
    sset = make()
    res = Edit(sset, [SetGap(Scope([0], [0], pedal_max=0), 100, "output", "mid")]).compute()
    assert (col(res, 0, 0, sset)[0], col(res, 0, 1, sset)[0]) == (1800, 1700)
    res = Edit(sset, [SetGap(Scope([0], [0], pedal_max=0), 100, "output", "high")]).compute()
    assert (col(res, 0, 0, sset)[0], col(res, 0, 1, sset)[0]) == (2000, 1900)


def test_set_off_writes_the_schedules_own_off_value_and_nothing_else():
    from ovtsuite.gearbox_edit import SetOff
    sset = make()
    sset.schedules[0].off = (10000.0, 8160.0)
    res = Edit(sset, [SetOff(Scope([0, 1], [0], pedal_min=40, pedal_max=60))]).compute()
    assert col(res, 0, 0, sset)[2:4] == [10000, 10000]
    assert col(res, 1, 0, sset) == col(Edit(sset).compute(), 1, 0, sset)   # no off value
    # an "off" cell is skipped by relative operations afterwards
    res = Edit(sset, [SetOff(Scope([0], [0], pedal_min=40, pedal_max=60)),
                      Offset(Scope([0], [0]), 10, "output")]).compute()
    assert col(res, 0, 0, sset)[2:5] == [10000, 10000, 2510]


def test_identical_check_warns_on_a_split_group_and_can_extend_the_edit():
    from ovtsuite.gearbox_edit import IdenticalCheck
    sset = make()
    twin = sched("SKL 12.0", 12, 0, [2000, 2000, 2100, 2300, 2500, 2700, 2900, 3000],
                 [1500, 1500, 1550, 1700, 1900, 2100, 2300, 2400])
    sset = ScheduleSet(sset.schedules + [twin], LINES)
    res = Edit(sset, [Offset(Scope([0], [0]), 50, "output")], [IdenticalCheck()]).compute()
    assert [f.code for f in res.findings] == ["identical"]
    assert "not edited: SKL 12.0" in res.findings[0].message
    res = Edit(sset, [Offset(Scope([0], [0]), 50, "output")],
               [IdenticalCheck(autofix=True)]).compute()
    assert res.findings[0].fixed and col(res, 3, 0, sset) == col(res, 0, 0, sset)
    # editing the whole group keeps it identical: nothing to say
    res = Edit(sset, [Offset(Scope([0, 3], [0]), 50, "output")], [IdenticalCheck()]).compute()
    assert res.findings == []


# ------------------------------------------------- blends and the log fit
def test_blend_round_trips_and_resolves_its_corners():
    from ovtsuite.gearbox_edit import Blend
    sset = make()
    b = Blend("D", [("SKL 11.0", 0.58), ("SKL 16.0", 0.43), ("SKL 99.0", 0.2)],
              programs=(10,), dsp=1, source="probe")
    assert Blend.from_dict(b.to_dict()) == b
    assert b.indices(sset) == [(0, 0.58), (1, 0.43)]


def _events(sset, weights, n=40, logs=4, lateness=50.0, latency=0.3, noise=0):
    """Synthetic steady shifts on a known blend."""
    import random
    from ovtsuite.gearbox_edit import Event
    from ovtsuite.gearbox_edit.model import interp
    rnd = random.Random(7)
    out = []
    for k in range(n):
        line = k % 2
        p = rnd.uniform(0, 100)
        thr = sum(w * interp(sset.schedules[i].pedal,
                             [sset.schedules[i].values()[r * 2 + line] for r in range(8)], p)
                  for i, w in weights.items())
        rate = rnd.uniform(50, 400) * (1 if line == 0 else -1)
        obs = thr + latency * rate + (lateness if line == 0 else -lateness) \
            + rnd.uniform(-noise, noise)
        out.append(Event("up" if line == 0 else "down", line, 4, p, obs,
                         program=10, dsp=1, log="log%d" % (k % logs), rate=rate))
    return out


def test_fit_recovers_known_weights_from_steady_shifts():
    from ovtsuite.gearbox_edit import fit_weights, lookup_from
    sset = make()
    grids = {None: {i: (s.pedal, s.values()) for i, s in enumerate(sset.schedules)}}
    evs = _events(sset, {0: 0.6, 1: 0.4})
    fit = fit_weights(sset, evs, [0, 1, 2], lookup_from(sset, grids))
    assert abs(fit.weights.get(0, 0) - 0.6) < 0.01 and abs(fit.weights.get(1, 0) - 0.4) < 0.01
    assert abs(fit.latency - 0.3) < 0.01 and fit.determinable
    assert fit.rms < 1


def test_fit_says_when_the_candidates_cannot_be_told_apart():
    from ovtsuite.gearbox_edit import fit_weights, lookup_from
    sset = make()
    twin = sched("SKL 12.0", 12, 0, [2000, 2000, 2100, 2300, 2500, 2700, 2900, 3000],
                 [1500, 1500, 1550, 1700, 1900, 2100, 2300, 2400])
    sset = ScheduleSet(sset.schedules[:2] + [twin], LINES)
    grids = {None: {i: (s.pedal, s.values()) for i, s in enumerate(sset.schedules)}}
    evs = _events(sset, {0: 0.3, 2: 0.3, 1: 0.4}, noise=30)
    fit = fit_weights(sset, evs, [0, 1, 2], lookup_from(sset, grids))
    assert not fit.determinable and "not determinable" in fit.verdict


def test_pedal_driven_shifts_only_bound_the_line():
    from ovtsuite.gearbox_edit import Event, evaluate, lookup_from
    sset = make()
    grids = {None: {i: (s.pedal, s.values()) for i, s in enumerate(sset.schedules)}}
    evs = _events(sset, {0: 1.0})
    # a kick: the 5>4 line jumped above the speed -- consistent; a lift: 4>5 below it
    evs += [Event("down", 1, 5, 50.0, 1500.0, steady=False, log="x"),
            Event("up", 0, 4, 50.0, 1500.0, steady=False, log="x")]
    rms, n, ok, nb = evaluate(sset, evs, [(0, 1.0)], lookup_from(sset, grids))
    assert n == 40 and nb == 2 and ok == 1          # the upshift at 1500 is below 4>5
    assert rms < 1


def test_gap_check_tolerance_ignores_a_rounding_step():
    sset = make()
    # 4>5 at row 0 is 2000 against 5>4 1500: a 1-rpm-short gap at min_gap 500
    res = Edit(sset, [Offset(Scope([0], [0], pedal_max=0), -1, "output")],
               [GapCheck(500, relative="fixed")]).compute()
    assert [f.code for f in res.findings] == ["gap"]
    res = Edit(sset, [Offset(Scope([0], [0], pedal_max=0), -1, "output")],
               [GapCheck(500, relative="fixed", tolerance=1.0)]).compute()
    assert res.findings == []

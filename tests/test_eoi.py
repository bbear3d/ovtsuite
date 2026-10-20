# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""End of injection (ovtsuite.eoi): the table-set blend, the EDC15/EDC16
injection hooks, and the plans (target EOI, duration, collapse)."""

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from ovtsuite import eoi  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
BIN = os.path.join(ROOT, "Reference", "Binaries")


def _session(*parts):
    p = os.path.join(BIN, *parts)
    if not os.path.exists(p):
        pytest.skip("%s not present" % parts[-1])
    from ovtsuite.session import TunerSession
    with contextlib.redirect_stdout(io.StringIO()):
        return TunerSession(p)


# ------------------------------------------------------------ the blend
def test_members_weight_by_the_fraction_not_a_plain_average():
    # selector output 0.25: three quarters of table 0, a quarter of table 1
    assert eoi.members(0.25, 6) == [(0, 0.75), (1, 0.25)]
    # an exact table number reads that table alone
    assert eoi.members(2.0, 6) == [(2, 1.0)]
    # the last table, and anything past it, reads the last table
    assert eoi.members(5.0, 6) == [(5, 1.0)]
    assert eoi.members(7.3, 6) == [(5, 1.0)]


def test_fmt_angle_is_atdc_signed():
    assert eoi.fmt_angle(4.04) == "4.0° ATDC"
    assert eoi.fmt_angle(-3.5) == "3.5° BTDC"
    assert eoi.fmt_angle(0.01) == "TDC"


# ------------------------------------------------------------- EDC16 PD
def test_edc16_lq_model_and_values():
    s = _session("03G906016LQ.bin")
    ms = eoi.models(s)
    assert len(ms) == 1 and not ms[0].missing
    m = ms[0]
    assert len(m.soi.tables) == 10 and len(m.duration.tables) == 7
    c = eoi.Calculator(m)
    cell = c.cell(90, 2000, 20)
    # SOI 3.96 BTDC (InjCrv_phiBas9), selector 3.84 -> 16 % MAP3 + 84 % MAP4
    assert cell.soi_btdc == pytest.approx(3.96, abs=0.02)
    assert [i for i, _ in cell.dur_members] == [3, 4]
    assert cell.eoi == pytest.approx(5.87, abs=0.05)
    g = c.grid(90)
    assert (len(g.rpm), len(g.q)) == (16, 14)
    assert eoi.selector_problems(m) == []


def test_edc16_gc_selector_placed_by_structure():
    # the OLS definition of p475_84_mf has no InjVlv_numMI1_CUR; the
    # supplement places it at the block base the pointer table holds
    s = _session("03G906016GC.bin")
    sel = next(e for e in s.entities if e.name == "InjVlv_numMI1_CUR")
    assert sel.addr == 0x1E68A0 + 2 + 2 * 6
    assert sel.values()[0] == [5.0, 4.0, 3.0, 2.0, 1.0, 0.0]
    assert not eoi.models(s)[0].missing


def test_target_eoi_moves_soi_and_is_one_undo_step():
    s = _session("03G906016LQ.bin")
    m = eoi.models(s)[0]
    c = eoi.Calculator(m)
    before = c.cell(90, 2000, 20)
    plan = eoi.plan_target_eoi(
        c, 90, lambda rpm, q: 4.0 if (rpm, q) == (2000.0, 20.0) else None)
    assert plan.n_cells == 1 and not plan.unreached
    eoi.apply(s, plan)
    c.refresh()
    after = c.cell(90, 2000, 20)
    assert after.eoi == pytest.approx(4.0, abs=0.05)   # one LSB is 0.023 deg
    assert after.soi_btdc > before.soi_btdc            # advanced
    s.undo()
    c.refresh()
    assert c.cell(90, 2000, 20).eoi == pytest.approx(before.eoi, abs=1e-9)


def test_duration_edit_moves_the_ecus_duration_exactly():
    s = _session("03G906016LQ.bin")
    c = eoi.Calculator(eoi.models(s)[0])
    before = c.cell(90, 2000, 20)
    plan = eoi.plan_duration(c, 90, [(2000.0, 20.0, before.duration + 0.5)])
    # both tables the selector blends there were written
    assert {e.name for e in plan.entities} == {
        "InjVlv_phiInjMI1_MAP3", "InjVlv_phiInjMI1_MAP4"}
    eoi.apply(s, plan)
    c.refresh()
    assert c.cell(90, 2000, 20).duration == pytest.approx(
        before.duration + 0.5, abs=0.03)


def test_collapse_keeps_warm_duration_at_the_kept_tables_breakpoints():
    s = _session("03G906016LQ.bin")
    c = eoi.Calculator(eoi.models(s)[0])
    t = c.dur_t[1]
    pts = [t.point(r, cc) for r in range(len(t.vals))
           for cc in range(len(t.vals[0]))]
    want = [c.cell(90, rpm, q).duration for rpm, q in pts]
    plan = eoi.plan_collapse(c, 1, 90)
    cold = eoi.collapse_error(c, plan, 1, 20)
    assert max(cold) > 0.2                     # the compensation that is lost
    eoi.apply(s, plan)
    c.refresh()
    assert set(c.dur_sel.vals[0]) == {1.0}
    got = [c.cell(90, rpm, q).duration for rpm, q in pts]
    assert max(abs(a - b) for a, b in zip(got, want)) < 0.03


# ------------------------------------------------------------- EDC15P PD
def test_edc15_gq_one_model_per_dataset():
    s = _session("EDC15", "038906019GQ.bin")
    ms = eoi.models(s)
    assert sorted(m.group for m in ms) == ["Automatic", "Manual 1", "Manual 2"]
    m = next(m for m in ms if m.group == "Manual 1")
    assert not m.missing and m.soi_btdc == -1.0
    c = eoi.Calculator(m)
    cell = c.cell(90, 2000, 20)
    # FB -5.04 (stored negative = BTDC), pump-map number 3.79:
    # 21 % zmwP_KF_P3 + 79 % zmwP_KF_P4
    assert cell.soi == pytest.approx(-5.04, abs=0.02)
    assert cell.soi_btdc == pytest.approx(5.04, abs=0.02)
    assert cell.index == pytest.approx(3.793, abs=0.01)
    assert [i for i, _ in cell.dur_members] == [3, 4]
    assert cell.eoi == pytest.approx(4.78, abs=0.05)


def test_edc15_late_soi_maps_are_where_the_code_reads_them():
    # p_pd_late names fnwSWGK0..9KR from the fnw setpoint routine (slots
    # 0x175E..0x1770, selector 0x1772): every dataset of the late images
    s = _session("EDC15", "038906019NJ_7331_9810.bin")
    ms = eoi.models(s)
    assert len(ms) == 2 and not any(m.missing for m in ms)
    for m in ms:
        base = 0x50000 if m.group == "Manual 1" else 0x70000
        assert [t.addr for t in m.soi.tables] == [
            base + 0x919A + 448 * i for i in range(10)]
    # the axis records the scan had read as maps are gone
    assert not any(e.name in ("KF_8856", "KF_887A", "KF_889A")
                   for e in s.entities)
    c = eoi.Calculator(next(m for m in ms if m.group == "Manual 1"))
    assert c.cell(90, 2000, 20).eoi == pytest.approx(8.40, abs=0.05)


def test_soi_set_tool_reproduces_the_damos_placement_on_gq():
    # the code-derived placement agrees with GQ's DAMOS-named definition
    p = os.path.join(BIN, "EDC15", "038906019GQ.bin")
    if not os.path.exists(p):
        pytest.skip("038906019GQ.bin not present")
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import add_soi_set_edc15 as T
    kb, _path, new, drop, _ev = T.build("p_pd", p)
    have = {t["name"]: t for t in kb["tables"]}
    assert not drop
    for t in new:
        assert (t["rel"], t["z_rel"]) == (have[t["name"]]["rel"],
                                          have[t["name"]]["z_rel"])


def test_vp37_has_no_model():
    s = _session("EDC15", "038906012A.bin")
    assert eoi.models(s) == []


def test_edc15_target_eoi_handles_the_negative_btdc_storage():
    s = _session("EDC15", "038906019GQ.bin")
    m = next(m for m in eoi.models(s) if m.group == "Manual 1")
    c = eoi.Calculator(m)
    before = c.cell(90, 2000, 20)
    plan = eoi.plan_target_eoi(
        c, 90, lambda rpm, q: 3.0 if (rpm, q) == (2000.0, 20.0) else None)
    eoi.apply(s, plan)
    c.refresh()
    after = c.cell(90, 2000, 20)
    assert after.eoi == pytest.approx(3.0, abs=0.05)
    assert after.soi_btdc > before.soi_btdc            # advanced
    assert after.soi < before.soi                      # stored more negative


def test_advance_for_target_includes_the_duration_change():
    s = _session("03G906016LQ.bin")
    c = eoi.Calculator(eoi.models(s)[0])
    cell = c.cell(90, 2000, 20)
    # a logged sample exactly as the model has it: EOI 5.87 ATDC, target 4
    adv = eoi.advance_for_target(c, 2000, 20, cell.soi_btdc, cell.duration, 4.0)
    assert adv > 0
    d, _, _ = c.duration(c.native(cell.soi_btdc + adv), 2000, 20)
    assert d - (cell.soi_btdc + adv) == pytest.approx(4.0, abs=0.01)
    # not the naive one degree per degree: the selector moves the duration
    assert abs(adv - (cell.eoi - 4.0)) > 0.01

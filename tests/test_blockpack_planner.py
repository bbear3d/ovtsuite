# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The logging-block planner on hand-built layouts (no binary, no Qt).

Fewest groups first; at the same count, filling a stock group beats creating
one; stock cells are never overwritten; nothing is planned without a gain."""
from ovtsuite.datalog.blockpack import (EMPTY, FILL, NEW, NO_SLOT, CellSource, Layout,
                                        NoGain, PlanItem, PresetPlan, Requirement,
                                        allocate, optimise)

E = EMPTY


def R(key, need=True, pin=None):
    return Requirement(key, (key,) if not pin else (), need, key, pin)


def picker(missing=()):
    def pick(req):
        if req.key in missing or not req.signals:
            return None
        return CellSource(value=hash(req.key) & 0xFF, signal=req.signals[0],
                          fingerprint=(0x01, 0x10))
    return pick


def stock_unchanged(layout, plan):
    for w in plan.writes:
        if w.kind == FILL:
            assert layout.cells[w.group][w.slot] is EMPTY, "stock cell overwritten"
        else:
            assert w.group in layout.free, "new cell outside a free group"


def test_three_full_groups_become_one_stock_and_one_new():
    lay = Layout(4, {1: ["rpm", "iq", "clt", "x"],
                     2: ["rpm", "boost", "bspec", "y"],
                     3: ["rpm", "n75", "atm", "z"]}, free=[40, 39])
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec"), R("n75"), R("atm", False), R("clt", False)]
    p = optimise("boost", "Boost", reqs, lay, [1, 2, 3], picker())
    assert isinstance(p, PresetPlan)
    assert p.groups == [1, 40]                    # lowest stock group wins the tie
    assert sorted(w.req for w in p.writes) == ["atm", "boost", "bspec", "n75"]
    assert all(w.kind == NEW for w in p.writes)
    assert set(r.key for r in reqs) <= set(p.covered)
    stock_unchanged(lay, p)


def test_filling_a_stock_group_beats_a_new_group_at_the_same_count():
    # five roles in four-cell groups: two groups at best. 1 + a new group
    # would do it, but 1 + 3 with one cell filled does it without creating one
    lay = Layout(4, {1: ["rpm", "iq", E, "w"],
                     2: ["rpm", "boost", E, "x"],
                     3: ["rpm", "bspec", "clt", "z"]}, free=[40])
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec"), R("clt")]
    p = optimise("k", "K", reqs, lay, [1, 2, 3], picker())
    assert isinstance(p, PresetPlan)
    assert p.groups == [1, 3] and not p.new_groups
    assert [(w.group, w.kind, w.req) for w in p.writes] == [(1, FILL, "boost")]
    stock_unchanged(lay, p)


def test_one_stock_group_filled_twice_beats_two_groups():
    lay = Layout(4, {1: ["rpm", "iq", E, E],
                     2: ["rpm", "boost", E, "x"],
                     3: ["rpm", "bspec", "y", "z"]}, free=[40])
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec")]
    p = optimise("k", "K", reqs, lay, [1, 2, 3], picker())
    assert isinstance(p, PresetPlan)
    assert p.groups == [1] and not p.new_groups
    assert sorted(w.req for w in p.writes) == ["boost", "bspec"]


def test_eight_slot_group_is_filled_through_its_empty_second_half():
    # EDC16: group 10's +127 row is empty -- four cells to fill, no new group
    lay = Layout(8, {1: ["rpm", "iq", "x", "y", E, E, E, E],
                     10: ["rpm", "bspec", "boost", "n75", E, E, E, E],
                     11: ["atm", "pedal", "clt", "z", E, E, E, E]}, free=[124, 123])
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec"), R("n75"),
            R("atm", False), R("pedal", False), R("clt", False)]
    p = optimise("boost", "Boost", reqs, lay, [1, 10, 11], picker())
    assert isinstance(p, PresetPlan)
    assert len(p.groups) == 1 and not p.new_groups
    assert {w.slot for w in p.writes} <= {4, 5, 6, 7}
    stock_unchanged(lay, p)


def test_no_gain_when_it_cannot_be_done_in_fewer():
    lay = Layout(4, {1: ["rpm", "iq", "clt", "a"], 2: ["boost", "bspec", "n75", "b"]},
                 free=[40, 39])
    reqs = [R("rpm"), R("iq"), R("clt"), R("boost"), R("bspec"), R("n75")]
    p = optimise("k", "K", reqs, lay, [1, 2], picker())
    assert isinstance(p, NoGain)
    assert "fewer" in p.reason


def test_single_group_preset_is_never_planned():
    lay = Layout(4, {1: ["rpm", "iq", E, E]}, free=[40])
    p = optimise("k", "K", [R("rpm"), R("iq")], lay, [1], picker())
    assert isinstance(p, NoGain)


def test_a_signal_with_no_other_source_keeps_its_stock_group():
    lay = Layout(4, {1: ["rpm", "iq", E, E], 2: ["egt", "x", "y", "z"], 3: ["boost", "a", "b", "c"]},
                 free=[40])
    reqs = [R("rpm"), R("iq"), R("egt"), R("boost")]
    p = optimise("k", "K", reqs, lay, [1, 2, 3], picker(missing={"egt"}))
    assert isinstance(p, PresetPlan)
    assert 2 in p.groups and len(p.groups) == 2
    assert "egt" not in {w.req for w in p.writes}


def test_pinned_cell_keeps_its_block():
    lay = Layout(4, {1: ["rpm", "iq", E, E], 2: ["", "x", "y", "z"], 3: ["boost", "a", "b", "c"]},
                 free=[40])
    reqs = [R("rpm"), R("iq"), R("pin:2/1", pin=(2, 0)), R("boost")]
    p = optimise("user:mine", "Mine", reqs, lay, [1, 2, 3], picker())
    assert isinstance(p, PresetPlan)
    assert 2 in p.groups and len(p.groups) == 2


def test_reserved_groups_are_read_but_never_filled_or_created():
    lay = Layout(4, {1: ["rpm", "iq", E, E], 2: ["boost", E, E, E], 3: ["bspec", "q", "r", "s"]},
                 free=[40], reserved={1, 2})
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec")]
    p = optimise("k", "K", reqs, lay, [1, 2, 3], picker())
    assert isinstance(p, PresetPlan)
    assert not {w.group for w in p.writes} & {1, 2}


def test_unserved_groups_are_not_used():
    lay = Layout(4, {1: ["rpm", "iq", "boost", "bspec"], 2: ["rpm", "x", "y", "z"],
                     3: ["iq", "a", "b", "c"], 4: ["boost", "bspec", "d", "e"]},
                 free=[40], unserved={1})
    reqs = [R("rpm"), R("iq"), R("boost"), R("bspec")]
    p = optimise("k", "K", reqs, lay, [2, 3, 4], picker())
    assert isinstance(p, PresetPlan)
    assert 1 not in p.groups


def test_out_of_free_groups_says_so():
    lay = Layout(4, {1: ["rpm", "a", "b", "c"], 2: ["iq", "d", "e", "f"], 3: ["boost", "g", "h", "i"]},
                 free=[])
    p = optimise("k", "K", [R("rpm"), R("iq"), R("boost")], lay, [1, 2, 3], picker())
    assert isinstance(p, NoGain)
    assert "free" in p.reason


def test_free_fill_adds_wants_without_adding_a_group():
    lay = Layout(4, {1: ["rpm", "a", "b", "c"], 2: ["iq", "d", "e", "f"], 3: ["boost", "g", "h", "i"],
                     5: ["clt", "j", "k", "l"]}, free=[40])
    reqs = [R("rpm"), R("iq"), R("boost"), R("clt", False)]
    p = optimise("k", "K", reqs, lay, [1, 2, 3], picker())   # clt is not read today
    assert isinstance(p, PresetPlan)
    assert p.groups == [40]
    assert p.extra == ["clt"] and "clt" in p.covered
    assert len(p.writes) == 4


def test_allocate_shares_cells_between_presets():
    lay = Layout(4, {1: ["rpm", "a", "b", "c"], 2: ["iq", "d", "e", "f"],
                     3: ["boost", "g", "h", "i"], 4: ["clt", "j", "k", "l"]}, free=[40, 39, 38])
    a = PlanItem("a", "A", (R("rpm"), R("iq"), R("boost")), (1, 2, 3))
    b = PlanItem("b", "B", (R("rpm"), R("iq"), R("clt")), (1, 2, 4))
    al = allocate([a, b], lay, picker())
    assert [p.key for p in al.plans] == ["a", "b"]
    pa, pb = al.plans
    assert len(pa.groups) == 1 and len(pb.groups) == 1
    # b reuses a's new group (rpm, iq already there) and fills its spare cell
    assert pb.groups == pa.groups
    assert [w.req for w in pb.writes] == ["clt"]
    assert len(al.writes) == 4
    wheres = [(w.group, w.slot) for w in al.writes]
    assert len(wheres) == len(set(wheres))


def test_allocate_reports_no_gain_presets_and_keeps_item_order():
    lay = Layout(4, {1: ["rpm", "iq", "clt", "a"], 2: ["boost", "c", "d", "e"],
                     3: ["n75", "f", "g", "h"], 4: ["tfuel", "iat", "i", "j"]}, free=[40])
    temps = PlanItem("temps", "Temps", (R("clt"), R("tfuel"), R("iat")), (1, 4))
    big = PlanItem("big", "Big", (R("rpm"), R("boost"), R("n75")), (1, 2, 3))
    al = allocate([temps, big], lay, picker())
    assert [p.key for p in al.plans] == ["big"]
    # Big gains more (3 -> 1) so it takes the only free group; Temps then
    # cannot get below 2
    assert [s.key for s in al.skipped] == ["temps"]
    assert al.plans[0].groups == [40]


def test_largest_gain_is_planned_first_when_free_groups_are_short():
    lay = Layout(4, {1: ["rpm", "a", "b", "c"], 2: ["iq", "d", "e", "f"],
                     3: ["boost", "g", "h", "i"], 4: ["n75", "j", "k", "l"],
                     5: ["clt", "m", "n", "o"], 6: ["egt", "p", "q", "r"]}, free=[40])
    small = PlanItem("small", "Small", (R("clt"), R("egt")), (5, 6))           # 2 -> 1
    big = PlanItem("big", "Big", (R("rpm"), R("iq"), R("boost"), R("n75")), (1, 2, 3, 4))  # 4 -> 1
    al = allocate([small, big], lay, picker())
    assert [p.key for p in al.plans] == ["big"]
    assert al.skipped[0].key == "small" and "free" in al.skipped[0].reason


def test_no_slot_positions_are_never_written():
    lay = Layout(8, {1: ["rpm", "iq", E, E, NO_SLOT, NO_SLOT, NO_SLOT, NO_SLOT],
                     2: ["boost", "x", "y", "z", NO_SLOT, NO_SLOT, NO_SLOT, NO_SLOT]}, free=[120])
    p = optimise("k", "K", [R("rpm"), R("iq"), R("boost")], lay, [1, 2], picker())
    assert isinstance(p, PresetPlan)
    assert all(lay.cells[w.group][w.slot] is EMPTY for w in p.writes if w.kind == FILL)

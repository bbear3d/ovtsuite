# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The ECU-side gearbox-intervention preset.

A downshift blip is a torque **increase**, so the increase channel is the one
this preset exists to watch. The first version did not have it: ``Gearbx_trqInc``
has no measuring descriptor, which was read as "the increasing intervention
cannot be measured" and led to a preset of one reduction channel plus two status
flags. That was wrong about the ECU. The coordinator's own pair does exist and
is already in group 20 -- ``CoVM_trqInc`` (from MSR) and ``CoVM_trqDec`` (from
ASR) -- so nothing had to be patched or even re-coded to see it.

The cost of the mistake was a measurement that could not settle its own
question: the dual log caught 26 downshifts with nothing on the reduction
channel or the flags, which is equally consistent with "nothing was asked" and
with "an increase was asked and this preset could not see it".
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.logtune.roles import (                           # noqa: E402
    RECIPES, ROLES, ROLE_LABELS, TCU_RECIPES, Recipe, plan_groups,
    recipe_groups, role_of_signal,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNING = os.path.join(REPO, "Reference", "Tunes", "ecu-lq-stg1.edc16")


def _recipe():
    return [r for r in RECIPES if r.key == "gbxintv"][0]


# --------------------------------------------------------------- definition
def test_the_preset_is_on_the_engine_side_not_the_tcu_side():
    """The logger picks TCU_RECIPES for a TCU tab and RECIPES otherwise."""
    assert _recipe().key not in {r.key for r in TCU_RECIPES}


def test_it_needs_both_torque_directions_and_both_flags():
    rec = _recipe()
    # trq_gbx alone is the decrease. Without trq_inc the preset cannot tell
    # "nothing was asked for" from "an increase was asked for", which is the
    # only question it is for.
    assert set(rec.needs) == {"trq_gbx", "trq_inc", "st_tsc", "st_dcs"}


def test_the_increase_channel_is_the_point_of_it():
    from ovtsuite.logtune.roles import ROLES
    assert ROLES["trq_inc"][0] == "CoVM_trqInc"
    assert "trq_inc" in _recipe().needs


def test_no_two_roles_claim_the_same_signal():
    """role_of_signal returns the first match, so a duplicate mapping makes one
    of the two roles unreachable -- which silently happened when a second role
    was pointed at FMTC_trqInr."""
    from ovtsuite.logtune.roles import ROLES
    firsts = [v[0] for v in ROLES.values()]
    assert len(firsts) == len(set(firsts))


def test_three_blocks_at_most():
    """A coasting downshift is over in a few tenths; a flag sampled twice
    across it cannot be told from one that never set."""
    assert _recipe().max_groups == 3


@pytest.mark.parametrize("role, signal", [
    ("st_tsc", "Gearbx_stTSC_mp"),
    ("st_dcs", "FrmMng_stDCS"),
    ("st_vdyn", "CoVM_stActDem"),
    ("st_can", "NetMng_stCAN"),
    ("trq_cruise", "CrCtl_trqDes"),
    ("trq_gbx", "Gearbx_trqDec"),
])
def test_each_new_role_maps_to_its_a2l_measurement(role, signal):
    assert ROLES[role][0] == signal
    assert role_of_signal(signal) == role
    assert ROLE_LABELS.get(role), "every role needs a label for the UI"


# ------------------------------------------------------------- the planner
class _FakeNamer:
    """The real group 9 / 21 / 213 layout of 03G906016LQ (p475_83)."""

    available = True

    def __init__(self, groups=None):
        self._g = groups if groups is not None else {
            6: [("Speed", "VSSCD_v"), ("Key", "CrCtl_stKeyStateSig"),
                ("Pedal", "APPCD_rAPP1UnFlt"), ("Key2", "CrCCD_stKeySig")],
            9: [("Engine speed", "Eng_nAvrg"), ("Cruise torque", "CrCtl_trqDes"),
                ("Gearbox reduction", "Gearbx_trqDec"), ("Limit", "CoEng_trqInrLim")],
            20: [("Engine speed", "Eng_nAvrg"), ("Inner torque", "FMTC_trqInr"),
                 ("ASR reduction", "CoVM_trqDec"), ("MSR increase", "CoVM_trqInc")],
            21: [("CAN", "NetMng_stCAN"), ("TSC", "Gearbx_stTSC_mp"),
                 ("Dynamics", "CoVM_stActDem"), ("MSR", "FrmMng_stDCS")],
            213: [("Engine speed", "Eng_nAvrg"), ("Pedal", "APPCD_rAPP1UnFlt"),
                  ("Speed", "VSSCD_v"), ("Gear", "Gearbx_stGear")],
            # a decoy: carries four WANTED roles and no needed one, which is
            # how the cover was tempted away from a need once before
            133: [("Engine speed", "Eng_nAvrg"), ("Air", "AFSCD_mAirPerCyl"),
                  ("Boost", "BPSCD_pFltVal"), ("Gear", "Gearbx_stGear")],
        }

    def populated_groups(self):
        return list(self._g)

    def slots(self, g):
        return len(self._g[g])

    def resolve(self, g, f):
        return self._g[g][f]


def test_it_plans_onto_blocks_this_ecu_answers():
    groups, missing = recipe_groups(_FakeNamer(), _recipe(), 3)
    assert missing == []
    assert groups == [9, 20, 21]


def test_it_does_not_plan_onto_group_213():
    """Group 213 carries pedal, road speed, rpm AND gear, so a plain greedy
    cover prefers it -- but on the P475 LQ it returned N/A for all four cells
    on every one of 3377 samples. The calibration's group map DEFINING a group
    is not the ECU SERVING it. Gear is left out of the wants, and
    ``datalog.served`` keeps the cover off 213 even when it would win."""
    rec = _recipe()
    assert "gear" not in rec.wants
    groups, _ = recipe_groups(_FakeNamer(), rec, 3)
    assert 213 not in groups
    assert max(groups) <= 30, "stay in the range this ECU answers"


def test_the_needs_alone_fit_in_the_cap():
    groups, missing = plan_groups(_FakeNamer(), list(_recipe().needs), 3)
    assert missing == []
    assert groups == [9, 20, 21]


def test_the_wants_never_cost_a_needed_role():
    """The defect that bit `discover`: the wants-driven cover is a fresh greedy
    plan, so it can reshuffle onto blocks richer in wanted roles and silently
    drop a need. Group 133 here is exactly that temptation."""
    rec = _recipe()
    groups, missing = recipe_groups(_FakeNamer(), rec, 3)
    carried = set()
    n = _FakeNamer()
    for g in groups:
        for f in range(n.slots(g)):
            r = role_of_signal(n.resolve(g, f)[1])
            if r:
                carried.add(r)
    assert carried.issuperset(rec.needs), "planner dropped a needed role"
    assert 133 not in groups


def test_a_need_on_a_fourth_block_is_reported_rather_than_exceeding_the_cap():
    spread = {
        1: [("Gearbox reduction", "Gearbx_trqDec"), (None, None)],
        2: [("TSC", "Gearbx_stTSC_mp"), (None, None)],
        3: [("Pedal", "APPCD_rAPP1UnFlt"), (None, None)],
        4: [("MSR", "FrmMng_stDCS"), (None, None)],
    }
    rec = Recipe("x", "x", ("trq_gbx", "st_tsc", "pedal", "st_dcs"), (), "", 3)
    groups, missing = recipe_groups(_FakeNamer(spread), rec, 3)
    assert len(groups) <= 3
    assert missing, "a role that does not fit must be reported, not dropped"


# ------------------------------------------- against the real calibration
@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_against_the_running_calibration():
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession

    sess = TunerSession(RUNNING)
    assert sess.platform == "edc16"
    namer = ChannelNamer(sess)
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")

    groups, missing = recipe_groups(namer, _recipe(), _recipe().max_groups)
    assert missing == []
    # 20 replaced 6: pedal and road speed were wanted, the increase channel is
    # needed, and with dual logging pedal/speed come off the TCU at full rate.
    assert groups == [9, 20, 21]


@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_the_increase_channel_really_is_in_a_block_on_this_calibration():
    """The correction, pinned: Gearbx_trqInc does not exist, but CoVM_trqInc
    does and is already served, so no patch or ESB edit is needed to see an
    increase request."""
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession

    namer = ChannelNamer(TunerSession(RUNNING))
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")

    seen = set()
    for g in namer.populated_groups():
        for f in range(namer.slots(g)):
            seen.add(namer.resolve(g, f)[1] or "")
    assert "Gearbx_trqDec" in seen, "the decreasing gearbox intervention"
    assert "Gearbx_trqInc" not in seen, "that name does not exist on this ECU"
    assert "CoVM_trqInc" in seen, "the INCREASE channel, which does"
    assert "CoVM_trqDec" in seen

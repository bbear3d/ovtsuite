# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Which measuring blocks the ECU serves, and the rows that are not groups.

The EDC16 0x21 handler (03G906016LQ @0x09B6EC) builds group g from the map
row whose Y-axis value is g (cells 1-4) and the row whose value is g + 127
(cells 5-8). Rows 128..254 are therefore second halves, never groups: every
block of 128 or more logged on the car answered N/A (133, 194, 213), while
group 6 answered eight cells, the last four being row 133's signals.
"""
import os

import pytest

from ovtsuite.datalog.served import FIRST_UNSERVED, unserved_for
from ovtsuite.logtune.roles import RECIPES, plan_groups, recipe_groups

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNING = os.path.join(REPO, "Reference", "Tunes", "ecu-lq-stg1.edc16")
LQ = os.path.join(REPO, "Reference", "Binaries", "03G906016LQ.bin")


# ------------------------------------------------------- the firmware rule
@pytest.mark.skipif(not os.path.exists(LQ), reason="needs 03G906016LQ.bin")
def test_group_6_carries_row_133_as_cells_5_to_8():
    """What the car returned for group 6 on 2026-10-10: vehicle speed, cruise
    keys/state, pedal, cruise keys -- then engine speed, air mass, boost and
    gear, which is row 133."""
    from ovtsuite.msblock import MeasuringBlocks
    from ovtsuite.session import TunerSession
    from ovtsuite.signals import SignalCatalog

    s = TunerSession(LQ)
    mb = MeasuringBlocks(s)
    cat = SignalCatalog(s.family, s)
    g6 = mb.group(6)
    names = [cat.info(c).name for c in g6.channels()]
    assert names == ["VSSCD_v", "CrCtl_stKeyStateSig", "APPCD_rAPP1UnFlt",
                     "CrCCD_stKeySig", "Eng_nAvrg", "AFSCD_mAirPerCyl",
                     "BPSCD_pFltVal", "Gearbx_stGear"]
    assert (g6.base_row, g6.ext_row) == (5, 132)


@pytest.mark.skipif(not os.path.exists(LQ), reason="needs 03G906016LQ.bin")
def test_only_groups_1_to_127_exist_and_a_half_empty_group_is_still_served():
    from ovtsuite.msblock import MeasuringBlocks
    from ovtsuite.session import TunerSession

    mb = MeasuringBlocks(TunerSession(LQ))
    assert [g.number for g in mb.groups()] == list(range(1, 128))
    assert mb.max_slots == 8
    g98 = mb.group(98)          # own row empty, row 225 populated
    assert g98.channels()[:4] == [255] * 4 and g98.populated


def test_groups_from_128_up_are_unserved_on_the_engine():
    class ChannelNamer:            # unserved_for keys on the class name
        available = True

        def populated_groups(self):
            return [1, 3, 6, 9, 21, 67, 99, 100, 115, 127, 133, 140]

    assert FIRST_UNSERVED == 128
    assert unserved_for(ChannelNamer()) == [133, 140]


def test_the_transmission_catalogue_is_never_filtered():
    """125, 225, 226 and 227 are the 6HP's own documented blocks."""
    class _Blocks6hp:
        available = True

        def populated_groups(self):
            return [1, 2, 3, 4, 5, 16, 125, 225, 226, 227]

    assert unserved_for(_Blocks6hp()) == []


def test_no_namer_says_nothing():
    assert unserved_for(None) == []

    class _Blind:
        available = False
    assert unserved_for(_Blind()) == []


# --------------------------------------------------------- in the planner
class _Namer:
    """Two clean blocks covering three roles between them, and one straggler
    covering all three at once -- the shape that made the plain greedy cover
    pick group 213 every time."""

    available = True
    MAP = {
        1: ["Eng_nAvrg", "InjCtl_qSetUnBal", None, "CTSCD_tClnt"],
        6: ["VSSCD_v", None, "APPCD_rAPP1UnFlt", None],
        213: ["Eng_nAvrg", "APPCD_rAPP1UnFlt", "VSSCD_v", "Gearbx_stGear"],
        133: [None, "AFSCD_mAirPerCyl", None, "Gearbx_stGear"],
    }

    def populated_groups(self):
        return sorted(self.MAP)

    def slots(self, g):
        return len(self.MAP[g])

    def resolve(self, g, f):
        return ("", self.MAP[g][f] or "", "")


def test_the_cover_prefers_clean_blocks_even_when_they_cover_less():
    """213 carries rpm+pedal+vss in one block; 1+6 need two. Coverage alone
    picks the straggler, which is the block that comes back N/A."""
    g, missing = plan_groups(_Namer(), ["rpm", "pedal", "vss"], 6,
                             avoid=[194, 213, 225, 226, 227, 228, 252])
    assert missing == []
    assert 213 not in g
    assert g == [1, 6]


def test_an_avoided_block_is_never_used_even_as_a_last_resort():
    """Banned, not last resort (Bear, 2026-10-10). The fallback is how the
    cruise preset landed on group 133 for the gear and logged N/A for a whole
    drive; a role nothing servable carries is reported as missing instead."""
    g, missing = plan_groups(_Namer(), ["rpm", "gear"], 6, avoid=[133, 213])
    assert missing == ["gear"]
    assert not ({133, 213} & set(g))


def test_without_avoid_the_old_behaviour_is_unchanged():
    g, _ = plan_groups(_Namer(), ["rpm", "pedal", "vss"], 6)
    assert g == [213]


# ---------------------------------------------- against the real calibration
@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_no_engine_preset_plans_a_row_above_127():
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession

    namer = ChannelNamer(TunerSession(RUNNING))
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")
    assert max(namer.populated_groups()) <= 127
    for rec in RECIPES:
        after, missing = recipe_groups(namer, rec, rec.max_groups,
                                       avoid=unserved_for(namer))
        assert missing == [], "%s lost a needed role: %s" % (rec.key, missing)
        assert all(g < 128 for g in after), "%s plans onto %s" % (rec.key, after)


@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_cruise_now_gets_the_gear_from_a_served_group():
    """The gear only lives in rows 133 and 213 -- cells 8 of groups 6 and 86,
    which the ECU does serve -- so cruise no longer has to give it up."""
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.logtune.roles import role_of_signal
    from ovtsuite.session import TunerSession

    namer = ChannelNamer(TunerSession(RUNNING))
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")
    rec = next(r for r in RECIPES if r.key == "cruise")
    groups, _ = recipe_groups(namer, rec, rec.max_groups, avoid=unserved_for(namer))
    carried = {role_of_signal(namer.resolve(g, f)[1])
               for g in groups for f in range(namer.slots(g))}
    assert set(rec.needs) <= carried
    assert "gear" in carried
    assert len(groups) <= rec.max_groups

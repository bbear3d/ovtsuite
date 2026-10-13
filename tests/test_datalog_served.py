# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring blocks the calibration defines but the ECU will not serve.

A populated row in ``UACC_RSigBlkGrp_MAP`` is not a promise. Measured on the
car: group 213 holds four ordinary signals (``Eng_nAvrg``,
``APPCD_rAPP1UnFlt``, ``VSSCD_v``, ``Gearbx_stGear``) and returned N/A on all
3377 samples of a real log, while groups 6, 9 and 21 from the same request
served normally. Nothing in the map tells them apart, there is no companion
max-group constant, and the dispatcher's bound check is not located -- so the
only thing the file offers is where the table stops being a table.
"""
import os

import pytest

from ovtsuite.datalog.served import (GAP_RATIO, MAX_TRAILING_FRACTION,
                                     trailing_outliers, unserved_for)
from ovtsuite.logtune.roles import RECIPES, plan_groups, recipe_groups

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNING = os.path.join(REPO, "Reference", "Tunes", "ecu-lq-stg1.edc16")


# ------------------------------------------------------- the boundary rule
def test_the_p475_map_breaks_where_it_is_measured_to_break():
    """The real populated sequence: 1..140 dense, a 54-row hole, then seven
    stragglers. 213 -- the block measured dead -- is among them, which is the
    only external check this rule gets."""
    g = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 15, 16, 17, 18, 20, 21, 22,
         23, 25, 26, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 40, 41, 42, 44,
         46, 51, 52, 55, 56, 62, 63, 64, 65, 67, 68, 69, 70, 71, 73, 74, 75, 80,
         81, 82, 86, 87, 89, 90, 91, 93, 95, 97, 100, 101, 102, 103, 104, 110,
         111, 115, 116, 117, 118, 125, 126, 127, 128, 131, 133, 134, 140, 194,
         213, 225, 226, 227, 228, 252]
    assert trailing_outliers(g) == [194, 213, 225, 226, 227, 228, 252]
    assert 213 in trailing_outliers(g)


def test_an_evenly_spread_map_is_left_entirely_alone():
    """A wrong answer costs more than no answer: it pushes a preset off a
    block the ECU serves perfectly well."""
    assert trailing_outliers(list(range(1, 60))) == []
    assert trailing_outliers([1, 5, 9, 13, 17, 21, 25, 29, 33]) == []


def test_a_gap_must_dwarf_the_ones_below_it():
    """Just being the widest gap is not enough -- in a ragged map some gap is
    always widest."""
    ragged = [1, 2, 3, 20, 21, 40, 41, 62, 63, 80]      # gaps 17,19,21,17
    assert trailing_outliers(ragged) == []
    cliff = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 60]         # gap 50 vs 1
    assert trailing_outliers(cliff) == [60]
    assert GAP_RATIO >= 3.0


def test_the_trailing_set_must_be_a_minority():
    """Half a map beyond the gap means the gap is not the end of the map."""
    half = list(range(1, 11)) + list(range(200, 211))
    assert trailing_outliers(half) == []
    assert MAX_TRAILING_FRACTION <= 0.25


def test_too_short_a_sequence_says_nothing():
    assert trailing_outliers([1, 2, 3, 200]) == []


def test_the_transmission_catalogue_is_never_filtered():
    """125, 225, 226 and 227 are the 6HP's own documented blocks, so the same
    rule there would discard exactly the ones that matter. That those numbers
    also appear in the ENGINE map is a reason to distrust them on the engine,
    not on the transmission."""
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


def test_groups_of_100_and_up_are_unserved_on_the_engine():
    """Every engine block of 100 or more ever logged on the car came back N/A
    (133, 194, 213); every one below 100 answered."""
    from ovtsuite.datalog.served import FIRST_UNSERVED

    class ChannelNamer:            # unserved_for keys on the class name
        available = True

        def populated_groups(self):
            return [1, 3, 6, 9, 21, 67, 99, 100, 115, 133, 140]

    assert FIRST_UNSERVED == 100
    assert unserved_for(ChannelNamer()) == [100, 115, 133, 140]


def test_without_avoid_the_old_behaviour_is_unchanged():
    g, _ = plan_groups(_Namer(), ["rpm", "pedal", "vss"], 6)
    assert g == [213]


# ---------------------------------------------- against the real calibration
@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_no_engine_preset_plans_a_group_of_100_or_more():
    from ovtsuite.datalog.channels import ChannelNamer
    from ovtsuite.session import TunerSession

    namer = ChannelNamer(TunerSession(RUNNING))
    if not namer.available:
        pytest.skip("no measuring-block map for this binary")
    avoid = set(unserved_for(namer))
    assert {133, 194, 213} <= avoid
    assert all(g >= 100 for g in avoid)
    for rec in RECIPES:
        after, missing = recipe_groups(namer, rec, rec.max_groups, avoid=sorted(avoid))
        assert missing == [], "%s lost a needed role: %s" % (rec.key, missing)
        assert all(g < 100 for g in after), "%s still plans onto %s" % (rec.key, after)


@pytest.mark.skipif(not os.path.exists(RUNNING), reason="needs the LQ tune")
def test_cruise_still_covers_everything_it_asked_for():
    """The gear only lives in 133 and 213, neither served, so it is the one
    wanted role cruise gives up; everything else stays."""
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
    assert set(rec.wants) - {"gear"} <= carried, "lost %s" % (set(rec.wants) - {"gear"} - carried)
    assert len(groups) <= rec.max_groups

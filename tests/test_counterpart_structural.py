# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Pairing a parameter across two calibrations, including unnamed ones.

The bug these pin: entities the knowledge base never named are not in
``by_key`` at all, so ``TunerSession.counterpart`` returned None for every one
of them. On a ZF 6HP image that is all 82 shift schedules and the
torque-converter tables -- the only tables anyone loads a comparison to look
at. The GUI's "show only what differs" hid them, a selected table showed no
overlay, and a generated patch left them out, all silently.

The second bug: the old name fallback, ``next(x for x in other.entities if
x.name == name)``, matched the FIRST unnamed entity when ``name`` was "" --
i.e. it would have compared one shift schedule against a different one.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from conftest import zf6hp_path                       # noqa: E402

from ovtsuite.session import TunerSession             # noqa: E402

STOCK = "3.0TFSI-Stock.bin"
STAGE1 = "3.0TFSI-Stage-1.bin"


@pytest.fixture(scope="module")
def pair():
    a = TunerSession(zf6hp_path(STAGE1))
    b = TunerSession(zf6hp_path(STOCK))
    return a, b


def _shift(session):
    return [e for e in session.entities
            if "Shift schedule" in (getattr(e, "label", "") or "")]


def test_unnamed_structural_entities_pair(pair):
    a, b = pair
    shift = _shift(a)
    assert shift, "no shift schedules detected in the reference image"
    unpaired = [e for e in shift if a.counterpart(b, e) is None]
    assert not unpaired, ("%d of %d shift schedules have no counterpart"
                          % (len(unpaired), len(shift)))


def test_pairing_is_to_the_same_schedule(pair):
    """Not merely non-None -- the right one.

    An empty name matched the first unnamed entity in the other file, which
    would pair SKL 0 against whatever happened to be detected first.
    """
    a, b = pair
    for e in _shift(a):
        cp = a.counterpart(b, e)
        assert cp.addr == e.addr, "%s paired to 0x%X" % (e.label, cp.addr)
        assert (cp.rows, cp.cols, cp.bits) == (e.rows, e.cols, e.bits)


def test_a_real_difference_is_visible(pair):
    """Stage 1 differs from stock in the schedules; the comparison must say so."""
    a, b = pair
    differing = 0
    for e in _shift(a):
        cp = a.counterpart(b, e)
        if bytes(a.binfile.data[e.addr:e.end]) != bytes(b.binfile.data[cp.addr:cp.end]):
            differing += 1
    assert differing, "Stage 1 and stock should differ in at least one schedule"


def test_named_entities_still_pair_by_identity(pair):
    """The KB path is unchanged -- identity is still name + reference address."""
    a, b = pair
    named = [e for e in a.entities if (e.name or "").strip()][:200]
    assert named
    paired = sum(1 for e in named if a.counterpart(b, e) is not None)
    assert paired > len(named) * 0.9


def test_counterpart_is_not_linear_in_entity_count(pair):
    """It used to walk by_key to find the entity's own key: O(n) per call, and
    O(n^2) over the whole-file diff the GUI runs to populate its tree."""
    import time
    a, b = pair
    ents = list(a.entities)
    t = time.time()
    for e in ents:
        a.counterpart(b, e)
    elapsed = time.time() - t
    assert elapsed < 2.0, ("a full counterpart pass over %d entities took %.1fs"
                           % (len(ents), elapsed))


def test_no_counterpart_across_unrelated_layouts():
    """Structural pairing must not invent a match between different software.

    Address plus geometry is the key, so a 6HP image and an EDC16 image share
    nothing and the pairing correctly yields None rather than a coincidence.
    """
    from conftest import BINARIES
    edc = os.path.join(BINARIES, "03G906016AB.bin")
    if not os.path.exists(edc):
        pytest.skip("no EDC16 reference binary")
    a = TunerSession(zf6hp_path(STAGE1))
    b = TunerSession(edc)
    shift = _shift(a)
    assert shift
    assert all(a.counterpart(b, e) is None for e in shift)

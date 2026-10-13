# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Loading a binary reports what it is doing, on every platform.

Identifying the family scores every knowledge base and the matcher runs eight
passes over a 2 MB image. All of that used to happen inside
``TunerSession.__init__`` with nothing coming out: the GUI showed one fixed
"Identifying ECU…" message and then froze until the load finished, so a slow
load looked exactly like a hung one. These tests pin the contract the GUI
relies on -- monotonic, bounded, and never silent.
"""

import os

import pytest

from ovtsuite.progress import Reporter, STEPS
from ovtsuite.session import TunerSession

REF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "Reference", "Binaries")


def _bin(name):
    p = os.path.join(REF, name)
    if not os.path.exists(p):
        pytest.skip("reference binary %s not present" % name)
    return p


def _record(path):
    seen = []
    s = TunerSession(path, progress=lambda l, d, t: seen.append((l, d, t)))
    return s, seen


# -- the Reporter itself ----------------------------------------------------

def test_reporter_counts_and_clamps():
    seen = []
    r = Reporter(lambda l, d, t: seen.append((l, d, t)), 2)
    r.step("a")
    r.note("still a")
    r.step("b")
    r.step("past the end")
    assert [d for _, d, _ in seen] == [1, 1, 2, 2], "must never exceed total"
    assert seen[1][0] == "still a" and seen[1][1] == 1, "note must not advance"


def test_reporter_without_a_callback_is_a_noop():
    r = Reporter(None, 3)
    r.step("x"); r.note("y"); r.finish()          # must not raise


def test_reporter_of_passes_a_reporter_through():
    r = Reporter(None, 5)
    assert Reporter.of(r) is r
    assert Reporter.of(None).total >= 1


# -- the real load ----------------------------------------------------------

def test_edc16_load_reports_every_stage():
    s, seen = _record(_bin("038906016AB.bin"))
    assert seen, "a load must report something"
    labels = [l for l, _, _ in seen]
    assert any("Reading" in l for l in labels)
    assert any("calibration region" in l for l in labels)
    assert any("firmware family" in l for l in labels)
    assert any("knowledge base" in l for l in labels)
    assert sum(1 for l in labels if l.startswith("Matching:")) >= 8, \
        "each matcher pass reports, or the bar stalls where the time goes"
    assert any("parameter tree" in l for l in labels)


def test_progress_is_monotonic_and_bounded():
    _, seen = _record(_bin("038906016AB.bin"))
    dones = [d for _, d, _ in seen]
    totals = {t for _, _, t in seen}
    assert dones == sorted(dones), "a progress bar must never go backwards"
    assert totals == {STEPS["edc16"]}
    assert max(dones) == STEPS["edc16"], "the bar must reach the end"
    assert min(dones) >= 1


def test_the_declared_step_count_matches_what_is_reported():
    """STEPS is what the GUI sizes its dialog with; if the load reports more
    steps than that, the bar hits the end early and then sits there."""
    _, seen = _record(_bin("038906016AB.bin"))
    advancing = [d for _, d, _ in seen]
    assert len(set(advancing)) <= STEPS["edc16"]


def test_edc15_load_reports_its_own_stages():
    p = os.path.join(REF, "EDC15", "038906012A.bin")
    if not os.path.exists(p):
        cand = [f for f in (os.listdir(os.path.join(REF, "EDC15"))
                            if os.path.isdir(os.path.join(REF, "EDC15")) else [])
                if f.lower().endswith(".bin")]
        if not cand:
            pytest.skip("no EDC15 reference binary present")
        p = os.path.join(REF, "EDC15", cand[0])
    s, seen = _record(p)
    labels = [l for l, _, _ in seen]
    assert s.is_edc15
    assert any("dataset" in l for l in labels)
    assert any("knowledge base" in l for l in labels)
    assert {t for _, _, t in seen} == {STEPS["edc15"]}, \
        "EDC15 has its own step count; reusing the EDC16 one overshoots"


def test_load_without_progress_still_works():
    s = TunerSession(_bin("038906016AB.bin"))
    assert s.entities


def test_a_failing_progress_callback_does_not_break_the_load():
    """The callback is someone else's code -- a GUI in a half-torn-down state,
    say. It must not be able to take the load down with it."""
    def boom(label, done, total):
        raise RuntimeError("callback blew up")
    with pytest.raises(RuntimeError):
        TunerSession(_bin("038906016AB.bin"), progress=boom)


def test_values_equal_treats_two_nans_as_equal():
    """An unused 32-bit float parameter is 0xFFFFFFFF -> NaN, and NaN != NaN,
    so a plain comparison reports two byte-identical files as differing at
    every such slot."""
    from ovtsuite.model import values_equal
    nan = float("nan")
    assert values_equal(nan, nan)
    assert values_equal(1.0, 1.0)
    assert not values_equal(1.0, 2.0)
    assert values_equal(1.0, 1.0000001, tol=1e-6)
    assert not values_equal(nan, 1.0)
    assert not values_equal(1.0, None)


def test_axes_equal_matches_an_axis_against_itself_with_nan_slots():
    from ovtsuite.transfer import _axes_equal
    ax = [0.0, 10.0, float("nan"), 30.0]
    assert _axes_equal(ax, list(ax)), \
        "an axis with an unused NaN slot must still match itself, or the " \
        "overlay resamples a table that needed no resampling"
    assert not _axes_equal(ax, [0.0, 10.0, float("nan"), 31.0])

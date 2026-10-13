# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Torque intervention during shifts: asked vs delivered.

The analysis exists to separate "the TCU asks for too little" from "the TCU asks
for the right amount and the engine does not deliver it" -- opposite fixes, and
a shift log cannot tell them apart by itself. The EDC16 gate
(``Gearbx_trqIncMax_C``) sat at 0 on this car, so every request produced nothing;
an auto-tuner that raised TCU maps would have written numbers with no effect.
These pin the measurement and, above all, the clipping warning.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from ovtsuite.logtune import shifttrq as T                       # noqa: E402
from ovtsuite.logtune.samples import Run                         # noqa: E402

RATIO_4TH, RATIO_3RD = T.RATIOS[3], T.RATIOS[2]
TUNE = os.path.join(os.path.dirname(__file__), "..",
                    "Reference", "Tunes", "6hp19-tdi20.6hp")


def _session():
    if not os.path.exists(TUNE):
        pytest.skip("reference tune not present")
    from ovtsuite.session import TunerSession
    return TunerSession(TUNE)


def _run(base_nm=50.0, bump_nm=60.0, rpm=2500.0, prog=10.0, hz=8.0,
         out=1000.0, pedal=0.0):
    """A steady 4th-gear cruise with one 4>3 at t=5 s and a torque bump."""
    n = int(10 * hz)
    t = [i / hz for i in range(n)]
    shift = int(5.0 * hz)
    turb, nout, trq, ped, pr, eng = [], [], [], [], [], []
    for i in range(n):
        third = i > shift
        nout.append(out)
        turb.append(out * (RATIO_3RD if third else RATIO_4TH))
        # the bump lands across the shift and decays
        trq.append(base_nm + (bump_nm if shift < i <= shift + 3 else 0.0))
        ped.append(pedal)
        pr.append(prog)
        eng.append(rpm)
    return Run(name="synthetic", t=t,
               data={"tcu_nturb": turb, "tcu_nout": nout, "tcu_trq": trq,
                     "tcu_pedal": ped, "tcu_prog": pr, "tcu_rpm": eng})


def test_it_finds_the_shift_by_ratio_and_measures_the_delivered_change():
    S = _session()
    rep = T.analyse(S, [_run(base_nm=50.0, bump_nm=60.0)])
    assert len(rep.events) == 1
    e = rep.events[0]
    assert (e.from_gear, e.to_gear, e.kind) == (4, 3, "down")
    assert e.baseline_nm == pytest.approx(50.0)
    assert e.delivered_nm == pytest.approx(60.0, abs=1.0)
    assert e.map_name == "KF_MEHAUPT43", "a 4>3 in a D program reads the normal map"


def test_a_sport_program_reads_the_sport_map():
    """Program 12 is a sport state per the controller's own tables."""
    S = _session()
    assert 12 in T.sport_programs(S) and 10 not in T.sport_programs(S)
    rep = T.analyse(S, [_run(prog=12.0)])
    assert rep.events[0].map_name == "KF_MEHAUPTSP43"


def test_the_gate_is_flagged_rather_than_silently_ignored():
    """The whole reason this is stage one and not an auto-tuner."""
    S = _session()
    rep = T.analyse(S, [_run(prog=12.0, base_nm=150.0, rpm=2500.0)], gate_nm=20.0)
    e = rep.events[0]
    assert e.asked_nm > 20.0
    assert e.clipped, "asked above the gate must be flagged"
    assert rep.needed_gate_nm == pytest.approx(e.asked_nm)
    txt = T.to_text(rep)
    assert "CLIPPED" in txt
    assert "Gearbx_trqIncMax_C" in txt, "the report must name the constant to raise"


def test_nothing_is_clipped_when_the_gate_is_above_every_request():
    S = _session()
    rep = T.analyse(S, [_run()], gate_nm=T.DEFAULT_GATE_NM)
    assert not any(e.clipped for e in rep.events)
    assert "CLIPPED" not in T.to_text(rep)


def test_a_moving_pedal_is_reported_so_the_event_can_be_discarded():
    S = _session()
    run = _run()
    # ramp ACROSS the shift window (t=5.0 s is index 40 at 8 Hz), not before it
    run.data["tcu_pedal"] = [0.0 if i < 40 else min(40.0, (i - 40) * 12.0)
                             for i in range(len(run))]
    rep = T.analyse(S, [run])
    assert rep.events[0].pedal_move is not None and rep.events[0].pedal_move > 5
    assert "pedal moved" in T.to_text(rep)


def test_a_log_without_torque_says_which_preset_to_use():
    S = _session()
    run = _run()
    del run.data["tcu_trq"]
    rep = T.analyse(S, [run])
    assert not rep.events
    assert any("no tcu_trq" in n for n in rep.notes)
    assert any("groups 1 + 9" in n for n in rep.notes)


def test_a_slow_log_is_called_out():
    """Below ~6 Hz a shift is one sample and the delivered figure is noise."""
    S = _session()
    rep = T.analyse(S, [_run(hz=3.4)])
    assert any("one sample" in n or "3.4 Hz" in n for n in rep.notes)


def test_lookup_interpolates_both_axes_and_clamps():
    S = _session()
    e, _ = T.find_map(S, 4, 3, False)
    ys, xs = e.y_values(), e.x_values()
    vals = e.values()
    assert T.lookup(e, ys[0], xs[0]) == pytest.approx(vals[0][0])
    assert T.lookup(e, ys[-1], xs[-1]) == pytest.approx(vals[-1][-1])
    assert T.lookup(e, ys[0] - 1000, xs[0] - 1000) == pytest.approx(vals[0][0]), "clamped"
    mid = T.lookup(e, (ys[0] + ys[1]) / 2, xs[0])
    assert min(vals[0][0], vals[1][0]) - 1 <= mid <= max(vals[0][0], vals[1][0]) + 1


# ---- the button in Tools > Tune from logs ---------------------------------
# ovtsuite.gui.HAVE_QT is the project's own probe: PySide6 can import while
# QtWidgets fails to load (no libEGL on a headless box).

from ovtsuite import gui as _g                                   # noqa: E402

_KEEP = []          # Qt parents must outlive the dialog or its children are freed


def _dialog(session):
    import types
    from PySide6 import QtWidgets
    from ovtsuite.logtune import gui_logtune as G
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    parent = QtWidgets.QWidget()
    d = G.LogTuneDialog(types.SimpleNamespace(primary=session), parent=parent)
    _KEEP.extend((parent, d))
    return d


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_the_report_is_offered_when_the_calibration_carries_the_family():
    d = _dialog(_session())
    assert d.trq_btn.isVisibleTo(d)
    assert "torque" in d.trq_btn.text().lower()


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_it_is_hidden_without_the_intervention_family():
    """Structural test, not a guess from the file name."""
    s = _session()
    s.entities = [e for e in s.entities if not (e.name or "").startswith("KF_MEHAUPT")]
    d = _dialog(s)
    assert not d.trq_btn.isVisibleTo(d)


@pytest.mark.skipif(not _g.HAVE_QT, reason="needs a working PySide6 QtWidgets")
def test_with_no_logs_it_explains_instead_of_crashing(monkeypatch):
    from PySide6 import QtWidgets
    d = _dialog(_session())
    seen = {}
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: seen.setdefault("msg", a[2]))
    d._shift_torque()
    assert "groups 1 + 9" in seen.get("msg", ""), "name the preset to use"

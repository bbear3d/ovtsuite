# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Degrees <-> microseconds for the End of injection tool (no ECU file
needed): the conversion, the assistant's unit argument, and the target
dialog's unit."""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6 import QtCore  # noqa: E402
from ovtsuite import eoi  # noqa: E402
from ovtsuite.ai import eoi_tools  # noqa: E402
from ovtsuite.ai.tools import ToolError  # noqa: E402


def test_conversion_is_one_revolution_per_60e6_over_rpm():
    assert eoi.deg_to_us(360.0, 1000.0) == pytest.approx(60000.0)
    assert eoi.deg_to_us(1.0, 2000.0) == pytest.approx(83.3333, abs=1e-3)
    assert eoi.deg_to_us(-6.0, 3000.0) == pytest.approx(-333.333, abs=1e-2)
    # the same angle is a longer time at a lower speed
    assert eoi.deg_to_us(10.0, 1000.0) > eoi.deg_to_us(10.0, 4000.0)


@pytest.mark.parametrize("rpm", [120.0, 750.0, 2000.0, 4500.0])
@pytest.mark.parametrize("deg", [-12.5, 0.0, 3.7, 40.0])
def test_round_trip(deg, rpm):
    assert eoi.us_to_deg(eoi.deg_to_us(deg, rpm), rpm) == pytest.approx(deg)


@pytest.mark.parametrize("rpm", [0.0, -50.0, None])
def test_no_time_without_a_running_engine(rpm):
    assert eoi.deg_to_us(5.0, rpm) is None
    assert eoi.us_to_deg(100.0, rpm) is None


def test_ai_units_argument():
    assert eoi_tools._units(None) is False
    assert eoi_tools._units("degrees") is False
    assert eoi_tools._units("Microseconds") is True
    assert eoi_tools._units("us") is True
    with pytest.raises(ToolError):
        eoi_tools._units("furlongs")
    assert eoi_tools._conv(False, 4.0, 2000) == 4.0
    assert eoi_tools._conv(True, 4.0, 2000) == pytest.approx(333.0, abs=0.6)
    assert eoi_tools._conv(True, 4.0, 0) is None


def test_target_dialog_takes_microseconds():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:                          # no Qt, or no GL libs
        pytest.skip("PySide6 is not usable here: %s" % exc)
    from ovtsuite import gui_eoi
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    calc = SimpleNamespace(soi_t=[], soi_members=lambda c: [],
                           model=SimpleNamespace(limiter=None))
    deg = gui_eoi.TargetDialog(None, calc, 90.0, False)
    us = gui_eoi.TargetDialog(None, calc, 90.0, False, True)
    assert "µs" in us.target.suffix() and "µs" not in deg.target.suffix()
    assert deg.result_values()[0] == 8.0
    us.target.setValue(-250)
    assert us.result_values()[0] == -250


# ------------------------------------------------- the window, on a stub model
def _stub_window(mp, QtWidgets):
    """An EoiDialog over a two-by-three synthetic model: rpm 0/1000/2000 rows,
    two quantity columns. EOI = duration - SOI with duration 10, SOI 6 deg."""
    from ovtsuite import gui_eoi
    xs, ys = [10.0, 20.0], [0.0, 1000.0, 2000.0]            # q columns, rpm rows

    class T:
        e = SimpleNamespace(name="SOI", x=None, y=None)
        vals = [[6.0, 6.0] for _ in ys]
        def __init__(self): self.xs, self.ys = xs, ys
        def point(self, r, c): return ys[r], xs[c]
        def weights(self, rpm, q=0.0): return []

    class Calc:
        by_coolant = False
        conditions_used = []
        model = SimpleNamespace(limiter=None, duration=SimpleNamespace(
            selector=SimpleNamespace(name="sel")))
        soi_t, dur_t = [T()], [T()]
        soi_cor = 0.0
        def __init__(self, *a, **k): pass
        def reference(self, cool): return T()
        def soi_members(self, cool): return [(0, 1.0)]
        def fuel_correction(self, *a): return 0.0
        def btdc(self, v): return v
        def fuel_q(self, rpm, q): return q
        def cell(self, cool, rpm, q, mem=None):
            return eoi.Cell(rpm, q, 6.0, [(0, 1.0)], False, 0.0, [(0, 1.0)],
                            10.0, 4.0, 6.0)

    model = SimpleNamespace(label="m", coolant_warm=90, missing=[], soi_sets=[],
                            notes=[], group=None, corrections=[],
                            duration=SimpleNamespace(tables=[], selector=SimpleNamespace(name="sel")))
    mp.setattr(eoi, "models", lambda s: [model])
    mp.setattr(eoi, "Calculator", Calc)
    mp.setattr(eoi, "selector_problems", lambda m: [])
    ws = QtWidgets.QWidget()
    ws.primary = None
    return gui_eoi.EoiDialog(ws), gui_eoi


def test_window_shows_types_and_copies_microseconds():
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:
        pytest.skip("PySide6 is not usable here: %s" % exc)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    with pytest.MonkeyPatch.context() as mp:
        d, gui_eoi = _stub_window(mp, QtWidgets)
        assert d.grid.item(1, 0).text() == "4"                   # degrees
        assert d.grid.item(1, 0).background().color().name().upper() == "#F1EFE8"
        b = next(b for b in d.unit_group.buttons() if b.property("unit") == "us")
        b.click()
        # 4 deg at 1000 rpm = 666.7 us; at 2000 rpm 333.3 us; no time at 0 rpm
        assert d.grid.item(1, 0).text() == "667"
        assert d.grid.item(2, 0).text() == "333"
        assert d.grid.item(0, 0).text() == "-"
        # EOI 4 deg = 667 us at 1000 rpm: late in the us bands (> 600), but
        # 333 us at 2000 rpm is the 200-400 band
        col = lambda r: d.grid.item(r, 0).background().color().name().upper()  # noqa: E731
        assert col(1) == "#F09595" and col(2) == "#FAEEDA"
        assert not d.grid.item(0, 0).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
        assert d.grid.item(1, 0).flags() & QtCore.Qt.ItemFlag.ItemIsEditable
        # typing 1000 us at 2000 rpm asks for EOI = 12 deg
        seen = []
        mp.setattr(eoi, "plan_target_eoi",
                   lambda calc, cool, want, *a: seen.append(want(2000.0, 10.0))
                   or SimpleNamespace(unreached=[], n_cells=0))
        mp.setattr(d, "_apply", lambda plan: 0)
        d.grid.item(2, 0).setText("1000 µs")
        assert seen == [pytest.approx(12.0)]
        d._copy()
        lines = QtWidgets.QApplication.clipboard().text().splitlines()
        assert lines[0].startswith("EOI us ATDC")
        assert lines[1].split("\t")[1:] == ["-", "-"]
        assert lines[3].split("\t")[1] == "333.3"
        d._show_info(2, 0)
        assert "333 µs after TDC" in d.info.text()
        d.mode_group.buttons()[3].click()                        # Table no.
        assert not d.unit_group.buttons()[0].isEnabled()
        assert d.grid.item(2, 0).text() == "0.00"

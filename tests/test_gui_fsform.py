# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""The Feature switches form a table hook can ask for (``FS_FORM``), and the
v/n band generator the gear hook shares with the gear-ratio editor."""
import math
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SPEC = {"title": "Gears", "states": ("set",), "fields": [
    {"key": "g1", "label": "1st", "kind": "number", "min": 0.0, "max": 20.0,
     "decimals": 3, "default": 0.0, "special": "not set"},
    {"key": "fd", "label": "Final drive", "kind": "number", "min": 0.0, "max": 10.0,
     "default": 0.0, "special": "not set"},
    {"key": "vn", "label": "Also the bands", "kind": "check", "default": True},
]}


@pytest.fixture(scope="module")
def app():
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_form_holds_the_fields_defaults_and_what_the_user_types(app):
    from ovtsuite.gui_fsform import FormBox
    box, seen = FormBox(), []
    box.changed.connect(lambda *a: seen.append(1))
    box.load(SPEC, {"fd": 3.389})
    assert box.keys() == ["g1", "fd", "vn"]
    assert box.values() == {"g1": 0.0, "fd": pytest.approx(3.389), "vn": True}
    assert not seen                                        # loading is not an edit
    box._widgets["g1"].setValue(3.778)
    box._widgets["vn"].setChecked(False)
    assert len(seen) == 2
    assert box.values()["g1"] == pytest.approx(3.778) and box.values()["vn"] is False


def test_a_number_at_its_minimum_reads_as_not_set(app):
    from ovtsuite.gui_fsform import FormBox
    box = FormBox()
    box.load(SPEC)
    assert box._widgets["g1"].specialValueText() == "not set"
    assert box.values()["g1"] == 0.0


def test_the_form_is_hidden_without_a_spec_and_rebuilt_for_another(app):
    from ovtsuite.gui_fsform import FormBox
    box = FormBox()
    box.load(SPEC)
    assert box.keys()
    box.load(None)
    assert box.keys() == []
    box.load({"fields": [SPEC["fields"][0]]})
    assert box.keys() == ["g1"]


def test_the_vn_generator_puts_crossovers_between_adjacent_gears():
    from ovtsuite import gears
    R = {1: 12.0, 2: 6.0, 3: 4.0, 4: 3.0, 5: 2.4, 6: 2.4}        # 6th repeats 5th
    plan = gears.vn_values(R, 1.95, 1.0, 11.0)
    got = dict(plan["values"])
    true = {g: 0.06 * 1.95 / R[g] for g in range(1, 6)}
    assert got["Gearbx_rVn1H_C"] == pytest.approx(math.sqrt(true[1] * true[2]))
    assert got["Gearbx_rVn5L_C"] == pytest.approx(math.sqrt(true[4] * true[5]))
    assert "Gearbx_rVn1L_C" not in got and "Gearbx_rVn5H_C" not in got   # floor and ceiling
    assert plan["gears"] == [1, 2, 3, 4, 5] and plan["placeholders"] == [6]
    assert "Gearbx_rVnRL_C" in got and got["Gearbx_rVnRL_C"] < got["Gearbx_rVnRH_C"]

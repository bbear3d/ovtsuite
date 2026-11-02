# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""tools/fit_dyno_air.py: rating from a file name, the air ramp, and a fit on a
stub model that has a known answer."""
import importlib.util
import os

import pytest

_P = os.path.join(os.path.dirname(__file__), "..", "tools", "fit_dyno_air.py")
_spec = importlib.util.spec_from_file_location("fit_dyno_air", _P)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


@pytest.mark.parametrize("name,kw", [
    ("Audi_A4_D1900_ATJ_R4V2T_85kW_0281010094.ori", 85.0),
    ("VW passat 1.9TDI AJM 115HP 038906019BK.ori", 115 * F.PS_KW),
    ("Seat leon 1.9TDI ARL 150 hp x.bin", 150 * F.PS_KW),
    ("Skoda fabia 105PS.bin", 105 * F.PS_KW),
    ("038906019CD_SG1042_nothing.bin", None),
    # the unit is followed by an underscore in the corpus's own names
    ("038906012AB_AFN_110hp_352258.bin", 110 * F.PS_KW),
    ("038906019CD_116hp_sg1042.ori", 116 * F.PS_KW),
    ("028906021HB_81kW_356006.bin", 81.0),
    ("038906019AT_ATD_100hp_366203.ori", 100 * F.PS_KW),
    ("038906019NT_160hp_378813.bin", 160 * F.PS_KW),
    ("0281001410_E4EDA020_ols.bin", None),
    ("038906019GK_4900_5342.bin", None),
])
def test_the_rating_comes_from_the_file_name(name, kw):
    got = F.rated_from_name(name)
    assert (got is None) if kw is None else got == pytest.approx(kw)


def test_a_rating_csv_matches_by_name_prefix_and_beats_the_name():
    table = {"Audi A4 1.9TDI": 84.6}
    assert F.rated_for("Audi A4 1.9TDI AJM 115HP 038906019AN.ori", table) == 84.6
    assert F.rated_for("Other 130HP.ori", table) == pytest.approx(130 * F.PS_KW)


def test_the_air_is_untouched_below_the_ramp_and_full_above_it():
    assert F.air_at(2000, 0.7, 0.2) == (F.BASE_VE, 1.0)
    ve, kept = F.air_at(4000, 0.7, 0.2)
    assert (ve, kept) == (pytest.approx(0.7), pytest.approx(0.8))
    ve, kept = F.air_at(3250, 0.7, 0.2)                      # half way
    assert (ve, kept) == (pytest.approx(0.8), pytest.approx(0.9))
    assert F.air_at(5000, 0.7, 0.2) == F.air_at(4000, 0.7, 0.2)


class _Cond:
    ve, baro = 0.9, 1000.0


class _Model:
    """Power rises with the air the wrapped ``_boost`` and ``cond.ve`` give."""

    def __init__(self):
        self.cond = _Cond()

    def _boost(self, rpm, q, cond):
        return 2000.0


def test_the_wrapped_boost_loses_gauge_pressure_and_sets_the_efficiency_then_undoes():
    m = _Model()
    undo = F.with_air(m, 0.8, 0.5)
    assert m._boost(4000, 40, m.cond) == pytest.approx(1500.0)   # 1000 + 1000 * 0.5
    assert m.cond.ve == pytest.approx(0.8)
    assert m._boost(2000, 40, m.cond) == pytest.approx(2000.0)
    undo()
    assert "_boost" not in vars(m)
    assert m._boost(4000, 40, m.cond) == 2000.0


def test_the_fit_finds_the_parameters_that_reproduce_the_rating(monkeypatch):
    """A stub whose power is its air supply, rated at what (0.8, 0.1) gives."""
    def power(model, ve=F.BASE_VE, loss=0.0):
        e, kept = F.air_at(4000, ve, loss)
        return 100.0 * e * kept
    monkeypatch.setattr(F, "peak_kw", power)
    rated = power(None, 0.8, 0.1)
    rows = [("a", object(), rated), ("b", object(), rated)]
    best, _ = F.fit(rows, F.frange(0.6, 0.95, 0.05), F.frange(0.0, 0.3, 0.05))
    sc, ve, loss = best
    assert sc == pytest.approx(0.0, abs=1e-12)
    assert power(None, ve, loss) == pytest.approx(rated)


def test_a_wild_file_cannot_pull_the_score_past_the_clip(monkeypatch):
    monkeypatch.setattr(F, "peak_kw", lambda m, ve=F.BASE_VE, loss=0.0: 100.0)
    assert F.score([("v10", None, 10.0)], 0.9, 0.0) == pytest.approx(F.CLIP ** 2)


def test_a_csv_row_for_the_same_software_number_rates_a_file_the_name_does_not():
    table = {"VW passat 1.9TDI AVF 130HP 038906019DS 0281010543 ": 95.6}
    assert F.software_number("038906019DS_AVF_130hp_360449.bin") == "038906019DS"
    assert F.rated_for("038906019DS_nothing_360449.bin", table) == 95.6
    # the name's own rating wins over a software-number match
    assert F.rated_for("038906019DS_AVF_100hp_1.bin", table) == pytest.approx(100 * F.PS_KW)
    assert F.rated_for("038906019GQ.bin", table) is None

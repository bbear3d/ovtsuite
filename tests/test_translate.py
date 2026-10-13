# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""German->English rendering of A2L descriptions."""

from ovtsuite.signal_names import translate_german as T


def test_english_untouched():
    for s in ["Upper hysteresis threshold - intake air temperature",
              "Map for determining the torque limiting factor",
              "Injection quantity setpoint", "Engine speed at idle"]:
        assert T(s) == s                       # no German marker -> unchanged


def test_compounds_translated():
    assert T("Korrekturkennfeld") == "Correction map"
    assert T("Filterzeitkonstante") == "Filter time constant"
    assert T("Kraftstofftemperatur") == "Fuel temperature"      # no stray 'e'
    assert T("Momentenbegrenzung") == "Torque limitation"
    assert T("Klimadruck") == "A/C pressure"
    assert "quantity" in T("Mehrfacheinspritzungsmenge").lower()
    assert "s quantity" not in T("Mehrfacheinspritzungsmenge").lower()


def test_no_substring_corruption():
    # 'temperatur' must not be rewritten inside English 'temperature'
    assert "temperature e" not in T("water temperature sensor")
    assert T("moment of inertia") == "moment of inertia"


def test_german_sentence():
    out = T("Sollwert der Drehzahl im Leerlauf")
    assert "setpoint" in out.lower() and "engine speed" in out.lower()
    assert "idle" in out.lower()
    assert "ä" not in out and "ö" not in out and "ü" not in out

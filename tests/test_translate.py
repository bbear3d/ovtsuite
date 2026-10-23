# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""German->English rendering of A2L descriptions."""

from ovtsuite import signal_names as sn
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


def test_german_words_in_half_translated_text():
    # A2L / KB texts that are mostly English but kept a German word
    assert T("Schwere counter limit for fault path Dfp_BrkCD") == \
        "Severity counter limit for fault path Dfp_BrkCD"
    assert T("CARB specific Fehlercode for fault path Dfp_AFSCD_Pl") == \
        "CARB specific fault code for fault path Dfp_AFSCD_Pl"
    assert "Umweltbedingung" not in T("SBR Umweltbedingung 4")


def test_ascii_umlauts_inflections_compounds():
    out = T("KTF Umweltbedingung 2 / Z: Uebernahme von Festwerten von 0 bis 65535")
    for de in ("Umweltbedingung", "Uebernahme", "Festwerten", " von ", " bis "):
        assert de not in out
    assert "linear" in T("ARD linearer G-Faktor").lower()
    assert T("Signalnummer") == "Signal number"
    assert T("Istgang") == "Actual gear"
    assert "general" in T("ARD Koeffizient D-Regler allgemein")


def test_english_and_code_words_untouched():
    for s in ["H2503_MOT7: Input signals from message MOT7",
              "Adaptation value is not used",
              "Minimum Duty cycle From PWM-Input From Refrigerant pressure",
              "pos. Offset On Torque loss At neg. Load change",
              "counter for unsuccessful regenerations [%]",
              "also fast", "Standard information"]:
        assert T(s) == s
    # an identifier that opens the text keeps its case
    out = T('jSasGlsRSS43_Fktn: "Applikationesgroessen Instanz jSasGlsRSS43"')
    assert out.startswith("jSasGlsRSS43_Fktn:") and "Instanz" not in out


def test_abbreviations_and_switch_states():
    assert "positive Gradient" in T("ARD Fuehrungsformer Ausrollen pos. Gradient")
    assert T("Anfangswert EIN").endswith("ON")


def test_category_headings_translated():
    # tree headings are translated where they are shown (gui / search)
    assert "Koordination" not in T("Antriebs-Koordination")
    out = T("Koordinator fuer Luftfilterventil Komponententreiber")
    assert "fuer" not in out and "Komponententreiber" not in out


# ---- 2026-10-22: gaps found on the DAMOS/A2L library (docs/CONVERTER.md)
def test_umlaut_text_finds_a_word_the_table_spells_with_oe():
    # the table has "oelpumpe"; the description writes "Ölpumpe"
    assert sn.translate_german("Ölpumpe liefert hohen Druck").startswith("Oil pump")


def test_compound_of_three_or_more_parts():
    W = sn._words()
    assert sn._compound("startzündwinkelrampe", W) == "start ignition angle ramp"
    assert sn._compound("ladedrucksollwertbegrenzung", W).startswith("boost pressure setpoint")
    # a short everyday word is not cut into nonsense
    assert sn._compound("warntexte", W) != "was texts"
    assert sn._compound("rückseite", W) != "reverse since"


def test_verb_stems_short_parts_and_participles():
    assert sn._lookup("heizbetrieb") == "heating operation"
    assert sn._lookup("ölpegel") == "oil level"
    assert sn._lookup("antriebsstrangaktivierungsart") == "drivetrain activation type"
    assert sn._lookup("begrenzenden") == "limiting"
    # a word that merely takes -en is no verb stem
    assert sn._lookup("sondercode") in (None, "special code")


def test_small_ist_and_soll_are_verbs():
    out = sn.translate_german("Faktor, der von der Überdosierung abhängt, ist negativ")
    assert "is negative" in out and "actual" not in out
    assert sn.translate_german("Ist-Drehzahl des Motors").startswith("Actual")


def test_library_words():
    for de, en in (("Kurbelgehäuse", "crankcase"), ("zulässig", "permissible"),
                   ("Speicherfähigkeit", "storage capacity"), ("Saughub", "intake stroke"),
                   ("Saugmotor", "naturally aspirated engine")):
        assert sn._lookup(de.lower()) == en, de

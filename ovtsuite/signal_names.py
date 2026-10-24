# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Human-readable names for Bosch EDC16 measuring-block signals.

The measuring-block catalog stores each signal by its Bosch A2L symbol
(e.g. ``Eng_nAvrg``). Those mnemonics are precise but cryptic, so this module
maps them to plain-English names. ``humanize()`` returns a friendly label and
keeps the original symbol in parentheses for reference, e.g.::

    Eng_nAvrg  ->  "Engine Speed (Eng_nAvrg)"

Names come first from a curated table (CURATED) built from the ECU's own German
signal descriptions; anything not curated falls back to a systematic decode of
the Bosch naming convention (module prefix + physical-quantity code), so new
firmware still yields a reasonable label instead of a bare symbol.
"""

import re as _re
from functools import lru_cache as _lru_cache

# --- German -> English glossary for A2L descriptions -------------------------
# EDC16 A2L long descriptions are frequently German, and German compound nouns
# concatenate roots (Korrektur+Kennfeld = correction map). Replacing known
# roots (longest first) with a space-padded English term and then collapsing
# whitespace renders most compounds readably, e.g. "Korrekturkennfeld" ->
# "correction map", "Filterzeitkonstante" -> "filter time constant".
_DE_TERMS = {
    "istwert": "actual value", "sollwert": "setpoint",
    "vorgabewert": "default value", "grenzwert": "limit value",
    "schwellenwert": "threshold", "schwellwert": "threshold",
    "abgleichwert": "trim value", "korrekturwert": "correction value",
    "maximalwert": "maximum value", "minimalwert": "minimum value",
    "ersatzwert": "substitute value", "messwert": "measured value",
    "kennfeld": "map", "kennlinie": "curve",
    "grundkennfeld": "base map", "grundkennlinie": "base curve",
    "korrekturkennfeld": "correction map",
    "korrekturkennlinie": "correction curve",
    "begrenzungskennfeld": "limiting map",
    "förderbeginn": "start of injection", "foerderbeginn": "start of injection",
    "spritzbeginn": "injection timing", "einspritzbeginn": "injection timing",
    "mehrfacheinspritzung": "multiple injection",
    "voreinspritzung": "pilot injection", "nacheinspritzung": "post injection",
    "haupteinspritzung": "main injection", "einspritzung": "injection",
    "einspritz": "injection", "spritz": "injection",
    "momentenbegrenzung": "torque limitation",
    "drehmoment": "torque", "moment": "torque",
    "begrenzung": "limitation", "begrenz": "limit",
    "drehzahl": "engine speed", "geschwindigkeit": "speed",
    "trägergeschwindigkeit": "carrier speed", "träger": "carrier",
    "ladedruck": "boost pressure", "saugrohrdruck": "manifold pressure",
    "umgebungsdruck": "ambient pressure", "raildruck": "rail pressure",
    "klimadruck": "a/c pressure", "druck": "pressure",
    "kraftstofftemperatur": "fuel temperature",
    "kühlmitteltemperatur": "coolant temperature",
    "ladelufttemperatur": "intake air temperature",
    "ansauglufttemperatur": "intake air temperature",
    "tanktemperatur": "tank temperature", "temperaturoffset": "temperature offset",
    "übertemperatur": "overtemperature", "temperatur": "temperature",
    "kraftstoff": "fuel", "luftmasse": "air mass", "luftmenge": "air quantity",
    "abgas": "exhaust", "gemisch": "mixture", "ladung": "charge",
    "kühlung": "cooling", "kühl": "cooling", "heizung": "heating",
    "heiz": "heating", "lüfternachlaufdauer": "fan run-on duration",
    "lüfter": "fan", "nachlauf": "run-on", "warmlauf": "warm-up",
    "filterzeitkonstante": "filter time constant",
    "zeitkonstante": "time constant", "sperrzeit": "lockout time",
    "totzeit": "dead time", "verzugszeit": "delay time",
    "verzögerung": "delay", "beschleunigung": "acceleration",
    "getriebetyp": "gearbox type", "getriebe": "gearbox",
    "gangwechsel": "gear change", "gang": "gear", "kupplung": "clutch",
    "leerlauf": "idle", "volllast": "full load", "teillast": "part load",
    "last": "load", "schubabschaltung": "overrun fuel cut", "schub": "overrun",
    "abschaltung": "shutoff", "abschalt": "shutoff", "abstellen": "shutdown",
    "freigabe": "enable", "anforderung": "request", "aktivierung": "activation",
    "überwachung": "monitoring", "regelung": "control", "steuerung": "control",
    "regel": "control", "anhebung": "increase", "absenkung": "reduction",
    "korrektur": "correction", "abgleich": "trim", "adaption": "adaptation",
    "initialisierung": "initialization", "überfüllschwelle": "overfill threshold",
    "überfüll": "overfill", "füll": "fill", "schwelle": "threshold",
    "grundwert": "base value", "grund": "base", "vorgabe": "default",
    "faktor": "factor", "kennwert": "characteristic value",
    "zähler": "counter", "dauer": "duration", "zeit": "time",
    "anzahl": "number of", "mindest": "minimum", "maximal": "maximum",
    "minimal": "minimum", "oberer": "upper", "obere": "upper",
    "unterer": "lower", "untere": "lower", "bremse": "brake", "brems": "brake",
    "luft": "air", "wert": "value", "abhängig": "dependent", "abh": "dependent",
    "mehrfach": "multiple", "differenz": "difference",
    "istdrehzahl": "actual engine speed", "solldrehzahl": "target engine speed",
    "menge": "quantity", "hinterlegt": "stored", "auswahl": "selection",
    "berechnung": "calculation", "erkennung": "detection",
    "klimaanlage": "a/c system", "klima": "a/c", "anlage": "system",
    "anzahl": "number of", "hysterese": "hysteresis", "schalter": "switch",
    "ventil": "valve", "geber": "sensor", "aktivierungsschwelle":
    "activation threshold", "deaktivierung": "deactivation",
    "aktiv": "active", "wirkungsgrad": "efficiency", "verhältnis": "ratio",
    "übersetzung": "ratio", "achse": "axis", "raddrehzahl": "wheel speed",
}
# Additional roots seen mainly in the more heavily-German hq family
# descriptions (Audi A6 136HP), whose A2L text is largely German.
_DE_TERMS.update({
    "umrechnung": "conversion", "umrechung": "conversion",   # (sic: XDF typo)
    "skalierung": "scaling", "grenze": "limit", "grenz": "limit",
    "rampensteigung": "ramp slope", "steigung": "slope",
    "abweichung": "deviation", "löschentprellung": "clear debounce",
    "löschung": "clearing", "löschen": "clear",
    "heilungsentprellung": "healing debounce", "fehlerheilung": "fault healing",
    "heilung": "healing", "fehlereinträgen": "fault entries",
    "fehlereintrag": "fault entry", "fehlereintr": "fault entry",
    "fehler": "fault", "priorität": "priority", "zuordnung": "assignment",
    "bedingung": "condition", "freischaltung": "activation",
    "stellgröße": "actuating value", "größe": "value",
    "plausibilisierung": "plausibility check", "plausibilität": "plausibility",
    "bestätigungsentprellung": "confirm debounce",
    "bestätigung": "confirmation", "bestätigten": "confirmed",
    "bestätigt": "confirmed", "tastverhältnis": "duty cycle",
    "verhältnisse": "conditions", "botschaftszähler": "message counter",
    "botschaft": "message", "kurzschluss": "short circuit",
    "richtung": "direction", "entprellung": "debounce", "entprell": "debounce",
    "änderung": "change", "während": "during", "zulässige": "permissible",
    "zulässig": "permissible", "atmosphären": "atmospheres",
    "atmosphäre": "atmosphere", "batteriespannung": "battery voltage",
    "spannung": "voltage", "verstärkung": "gain",
    "umschaltung": "switchover", "umschalt": "switchover", "masse": "mass",
    "zündung": "ignition", "zünd": "ignition", "leckage": "leakage",
    "prüfung": "check", "prüf": "check", "messung": "measurement",
    "fahrverhalten": "driving behavior", "fahrerwunsch": "driver demand",
    "fahrdynamik": "vehicle dynamics", "fahrzeugmasse": "vehicle mass",
    "fahrzeug": "vehicle", "fahrer": "driver",
    "referenz": "reference", "konventionell": "conventional",
    "zuganteil": "traction share", "zugbetrieb": "traction mode",
    "schubbetrieb": "overrun mode", "reduziert": "reduced",
    "reduzier": "reduce", "zustand": "state", "funktion": "function",
    "wärmeübertragung": "heat transfer", "übertragung": "transfer",
    "koeffizient": "coefficient", "wärme": "heat", "anteil": "share",
    "berücksichtigung": "consideration", "eingriff": "intervention",
    "eingriffe": "interventions", "anhebungs": "increase",
    "kennung": "identifier", "aktuell": "current", "aktuelle": "current",
    "fahrbahn": "roadway", "viskosität": "viscosity", "dynamisch": "dynamic",
    "dynamische": "dynamic", "kinematisch": "kinematic",
    "kinematische": "kinematic", "abgasgegendruck": "exhaust back pressure",
    "gegendruck": "back pressure", "turbine": "turbine",
    "verdichter": "compressor", "lader": "charger", "welle": "shaft",
    "drossel": "throttle", "drosselklappe": "throttle valve",
    "einlass": "intake", "auslass": "exhaust", "zylinder": "cylinder",
    "rohwert": "raw value", "roh": "raw", "gefiltert": "filtered",
    "gefilterte": "filtered", "gemittelt": "averaged", "mittelwert": "mean value",
    "höhe": "altitude", "höhen": "altitude", "steigungswinkel": "slope angle",
    "winkel": "angle", "strecke": "distance", "weg": "path",
    "erhöhung": "increase", "erhöh": "increase", "länge": "length",
    "kommunikationsausfall": "communication failure",
    "kommunikation": "communication", "ausfall": "failure", "defekt": "fault",
    "treiberstrom": "driver current", "treiber": "driver", "strom": "current",
    "partikelfilterregeneration": "DPF regeneration",
    "partikelfilter": "particulate filter", "lesbar": "readable",
    "glied": "element", "gliedes": "element", "geheilt": "healed",
    "heilen": "heal", "entprellzeit": "debounce time",
})
# Vocabulary from the fully-German 03G906016 (P447HAS7) DAMOS A2L -- the p350/
# p475 A2Ls are English, so these roots were previously untranslated.
_DE_TERMS.update({
    "physikalischer": "physical", "physikalische": "physical",
    "physikalisch": "physical", "darstellung": "representation",
    "auflösung": "resolution", "hauptsächlich": "mainly",
    "diagnoseschnittstelle": "diagnostic interface",
    "schnittstelle": "interface", "konvertierung": "conversion",
    "konvertier": "convert", "kundenspezifisch": "customer-specific",
    "spezifisch": "specific", "kunden": "customer", "freischnitt": "release",
    "rampengradient": "ramp gradient", "einheit": "unit",
    "einstellung": "setting", "einstell": "set", "filterung": "filtering",
    "regenerationsbetrieb": "regeneration mode", "regeneration": "regeneration",
    "umgebungs": "ambient", "umgebung": "ambient", "bildung": "formation",
    "ermittlung": "determination", "leistungs": "power", "leistung": "power",
    "bestimmung": "determination", "linearisierungs": "linearization",
    "linearisierung": "linearization", "verhalten": "behavior",
    "anpassung": "adaptation", "invertierungsparameter": "inversion parameter",
    "invertierung": "inversion", "invertier": "invert",
    "einschalt": "switch-on", "ausschalt": "switch-off",
    "eingeschalt": "switched-on", "schrittweite": "step size",
    "abstellpfad": "shutdown path", "abstell": "shutdown", "pfadname": "path name",
    "pfaden": "paths", "pfade": "paths", "pfad": "path", "verwendung": "use",
    "generatorerregung": "alternator excitation", "erregung": "excitation",
    "sensorversorgungs": "sensor supply", "sensorversorgung": "sensor supply",
    "versorgungs": "supply", "versorgung": "supply", "umwandlung": "conversion",
    "wunsch": "demand", "umsetzungs": "conversion", "umsetzung": "conversion",
    "ableitung": "derivative", "nachbehandlung": "aftertreatment",
    "motorschutz": "engine protection", "gewichtungs": "weighting",
    "gewichtung": "weighting", "normierung": "normalization",
    "kopplung": "coupling", "schnee": "snow", "ölniveau": "oil level",
    "ölstands": "oil level", "ölstand": "oil level", "öl": "oil",
    "kältemittel": "refrigerant", "steuergerätes": "control unit",
    "steuergerät": "control unit", "glühkerze": "glow plug", "glüh": "glow",
    "frühverstellung": "advance", "verstellung": "adjustment",
    "verdrängung": "displacement", "strömungswiderstand": "flow resistance",
    "widerstand": "resistance", "strömung": "flow", "kapazität": "capacity",
    "überhitzungsschutz": "overheat protection", "überhitzung": "overheating",
    "athmosphären": "atmospheres", "athmosphäre": "atmosphere",
    "atmosphären": "atmospheres", "losreiß": "break-away", "ruß": "soot",
    "großsignalbereich": "large-signal range", "großsignal": "large signal",
    "großen": "large", "großer": "large", "groß": "large", "höchst": "maximum",
    "irregulär": "irregular", "verschleißäquivalent": "wear equivalent",
    "verschleiß": "wear", "pumpenhüb": "pump stroke", "pumpenhub": "pump stroke",
    "rückmeldeleitung": "feedback line", "rückmelde": "feedback",
    "leitung": "line", "paßwort": "password", "passwort": "password",
    "dämpfung": "damping", "krümmer": "manifold", "ausgäng": "output",
    "ausgang": "output", "störungsregler": "disturbance controller",
    "störung": "disturbance", "regler": "controller", "gültigkeit": "validity",
    "gültig": "valid", "überschreitung": "exceedance", "überschreit": "exceed",
    "unterschreitung": "undershoot", "zurücksetz": "reset", "rücksetz": "reset",
    "rückführung": "recirculation", "rückführ": "recirculation",
    "übertragenen": "transmitted", "übertragen": "transmitted",
    "zusätzlich": "additional", "übertreten": "exceed",
    "meßglied": "measuring element", "meß": "measurement",
    "durchzuführend": "to be performed", "durchführ": "perform",
    "führungsformer": "setpoint shaper", "führung": "guidance",
    "öffnen": "open", "öffnung": "opening", "schließen": "close",
    "muß": "must", "endgültig": "final", "stationär": "steady-state",
    "überwachte": "monitored", "überwacht": "monitored", "auswähl": "select",
    "pausibilitäts": "plausibility", "invertiert": "inverted",
    "vorsteuerung": "feedforward", "vorsteuer": "feedforward",
    "nachlaufzeit": "run-on time", "beladung": "loading", "belad": "load",
    "aschemasse": "ash mass", "asche": "ash", "abbrand": "burn-off",
    "aufheizung": "heat-up", "aufheiz": "heat-up", "abkühl": "cool-down",
    "sollmoment": "target torque", "istmoment": "actual torque",
    "wandlung": "conversion", "beaufschlagung": "application",
    "einströmenden": "inflowing", "strecke": "distance",
    "endwert": "final value", "startwert": "start value",
    "raildruckes": "rail pressure", "gaspedal": "accelerator pedal",
    "fahrpedalgradient": "accelerator pedal gradient",
    "fahrpedal": "accelerator pedal", "notfahrbetrieb": "limp-home mode",
    "notlauf": "limp-home", "transitionsfunktion": "transition function",
    "transition": "transition", "unplausibilität": "implausibility",
    "aufhebung": "cancellation", "prozentänderung": "percent change",
    "prozent": "percent", "diagnose": "diagnosis", "diagnostisch": "diagnostic",
    "drallniveausteuerung": "swirl level control", "drallniveau": "swirl level",
    "drallklappe": "swirl flap", "drall": "swirl", "differenzdruck": "differential pressure",
    "sauerstoffkonzentration": "oxygen concentration", "sauerstoff": "oxygen",
    "lambdasonde": "lambda sensor", "nockenwellensignal": "camshaft signal",
    "kurbelwellen": "crankshaft", "nockenwelle": "camshaft", "kurbelwelle": "crankshaft",
    "schleppbeschleunigung": "drag acceleration", "schlepp": "drag",
    "klemmend": "sticking", "transient": "transient",
    "fahrpedal": "accelerator pedal", "luftmassenmesser": "air mass meter",
    "fahrgeschwindigkeitsgeber": "vehicle speed sensor",
    "getriebedrehzahlsensor": "transmission speed sensor",
    "getriebedrehzahl": "transmission speed", "rauchbegrenzungsmenge":
    "smoke limit quantity", "rauchbegrenzung": "smoke limitation", "rauch": "smoke",
    "ladedruckschlauch": "boost pressure hose", "ladedruck": "boost pressure",
    "schlauch": "hose", "steuerungslogik": "control logic", "logik": "logic",
    "wegfahrsperre": "immobilizer", "sperre": "lock", "lernverfahren":
    "learning procedure", "verfahren": "procedure", "lernwert": "learned value",
    "lern": "learning", "nockenwellendrehzahl": "camshaft speed",
    "kurbelwellendrehzahl": "crankshaft speed", "haupteinspritzkorrektur":
    "main injection correction", "startbasismoment": "start base torque",
    "kraftstofftemperaturkorrektur": "fuel temperature correction",
    "luftaufwand": "air charge", "aufwand": "effort", "abgleichwerte":
    "adjustment values", "motorkennlinie": "engine map", "mengenkoordination":
    "quantity coordination", "gangwechsel": "gear change", "filterzeit": "filter time",
    "geber": "sensor", "messer": "meter",
    "motormechanikschutz": "engine mechanics protection",
    "mechanikschutz": "mechanics protection", "mechanik": "mechanics",
    "schutz": "protection", "bauteileschutz": "component protection",
    "nockenwellenposition": "camshaft position",
    "kurbelwellenposition": "crankshaft position",
    "nockenwellensensor": "camshaft sensor", "kurbelwellensensor": "crankshaft sensor",
    "bauteilschutz": "component protection", "bauteil": "component",
})
# P447 A2L long-tail German (incl. ss/sharp-s forms the ascii loop cannot
# derive from an umlaut key).
_DE_TERMS.update({
    "kurzschluß": "short circuit", "einlaßkanal": "intake port",
    "einlaßventil": "intake valve", "auslaßventil": "exhaust valve",
    "einlaß": "intake", "auslaß": "exhaust", "tastverhätnis": "duty cycle",
    "oberhalb": "above", "unterhalb": "below", "gedrückter": "pressed",
    "gedrückt": "pressed", "förder": "fuel delivery", "abwärts": "downward",
    "aufwärts": "upward", "rückwärts": "reverse", "längs": "longitudinal",
    "nächsten": "next", "nächste": "next", "rampenförmig": "ramp-shaped",
    "überschritten": "exceeded", "unterschritten": "undershot",
    "ermöglichen": "enable", "ermöglicht": "enables",
    "geräuschunterdrückung": "noise suppression",
    "unterdrückung": "suppression", "unterdrück": "suppress",
    "geräusch": "noise", "gewünschtes": "desired", "gewünschte": "desired",
    "gewünscht": "desired", "möglicher": "possible", "mögliche": "possible",
    "möglich": "possible", "rücknahme": "withdrawal", "erwärmung": "warming",
    "erwärm": "warm", "bezüglich": "regarding", "güteklasse": "quality class",
    "güte": "quality", "größter": "largest", "größte": "largest",
    "größer": "larger", "überblenden": "cross-fade", "überblend": "cross-fade",
    "zähnchenlern": "tooth-learning", "zähnchen": "tooth", "zähl": "count",
    "taskdurchläufe": "task passes", "durchläufe": "passes",
    "durchlauf": "pass", "proportionaltitätskonstante": "proportionality constant",
    "proportionalität": "proportionality", "ändern": "change", "änder": "change",
    "störgröße": "disturbance", "stör": "disturbance",
    "krümmerdruck": "manifold pressure", "krümmertemperatur": "manifold temperature",
    "hochlauf": "run-up", "auslauf": "coast-down", "anlauf": "start-up",
})




def _ascii_umlaut(s: str) -> str:
    """Transliterate umlauts the way the hq XDF does (ü->ue, ä->ae, ...)."""
    return (s.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae")
             .replace("ß", "ss"))


# Register the ASCII-transliterated spelling (fuer, ueberwachung, kuehlmittel,
# ...) of every umlaut key, since some XDFs -- notably the hq family -- store
# German descriptions with ue/ae/oe/ss instead of the umlaut characters.
for _k, _v in list(_DE_TERMS.items()):
    _a = _ascii_umlaut(_k)
    if _a != _k and _a not in _DE_TERMS:
        _DE_TERMS[_a] = _v
# Sort longest-first so compounds match before their roots.
_DE_ORDERED = sorted(_DE_TERMS.items(), key=lambda kv: -len(kv[0]))

# "Strong" roots are unambiguously German -- they are not substrings of common
# English words -- so their presence marks a word as German worth translating.
# Ambiguous roots (temperatur inside "temperature", moment, druck, wert, zeit,
# last, filter, start, offset, grund, faktor) are translated ONLY once a word is
# already known to be German, so English text is never corrupted.
_STRONG = {
    "kennfeld", "kennlinie", "drehzahl", "sollwert", "istwert", "grenzwert",
    "schwellenwert", "schwellwert", "vorgabewert", "abgleichwert",
    "korrekturwert", "grundwert", "kennwert", "ersatzwert", "messwert",
    "maximalwert", "minimalwert", "förder", "foerder", "einspritz", "spritz",
    "begrenzung", "begrenz", "ladedruck", "saugrohrdruck", "umgebungsdruck",
    "raildruck", "klimadruck", "kraftstoff", "kühl", "kühlung", "heizung",
    "heiz", "lüfter", "nachlauf", "warmlauf", "zeitkonstante", "sperrzeit",
    "totzeit", "verzugszeit", "verzögerung", "beschleunigung", "getriebe",
    "gangwechsel", "kupplung", "leerlauf", "volllast", "teillast",
    "schubabschaltung", "schub", "abschaltung", "abschalt", "abstellen",
    "freigabe", "anforderung", "aktivierung", "überwachung", "regelung",
    "steuerung", "regel", "anhebung", "absenkung", "korrektur", "abgleich",
    "adaption", "initialisierung", "überfüll", "überfüllschwelle", "füll",
    "schwelle", "grundkennfeld", "grundkennlinie", "korrekturkennfeld",
    "korrekturkennlinie", "begrenzungskennfeld", "übertemperatur", "gemisch",
    "luftmasse", "luftmenge", "menge", "zähler", "gang", "getriebetyp",
    "mehrfach", "abhängig", "träger", "trägergeschwindigkeit", "geschwindigkeit",
    "kraftstofftemperatur", "kühlmitteltemperatur", "ladelufttemperatur",
    "ansauglufttemperatur", "tanktemperatur", "temperaturoffset", "klima",
    "hinterlegt", "auswahl", "berechnung", "erkennung", "anzahl", "mindest",
    "förderbeginn", "foerderbeginn", "spritzbeginn", "einspritzbeginn",
    "voreinspritzung", "nacheinspritzung", "haupteinspritzung", "einspritzung",
    "mehrfacheinspritzung", "momentenbegrenzung", "drehmoment", "saugrohr",
    "kühlmittel", "ladeluft", "solldrehzahl", "istdrehzahl", "brems",
}
# also treat the ASCII-transliterated spellings and a few hq-specific markers
# as strong German signals so ASCII-only descriptions get translated.
_STRONG |= {_ascii_umlaut(_w) for _w in list(_STRONG)}
_STRONG |= {"fuer", "grenze", "skalierung", "umrechnung", "umrechung",
            "abweichung", "entprellung", "prioritaet", "zuordnung",
            "freischaltung", "botschaft", "kurzschluss", "steigung",
            "fahrverhalten", "fahrerwunsch", "fahrzeug", "fahrdynamik",
            "referenz", "reduziert", "zustand", "funktion", "konventionell",
            "zuganteil", "zugbetrieb", "uebertragung", "beruecksichtigung",
            "eingriff", "waerme", "fahrbahn", "viskositaet", "dynamische",
            "kinematische", "gegendruck", "verdichter", "drosselklappe",
            "rohwert", "gefilterte", "mittelwert", "erhoehung", "laenge",
            "ausfall", "treiberstrom", "partikelfilter", "kommunikation",
            "entprellzeit", "defekt",
            "physikalisch", "darstellung", "hauptsaechlich",
            "konvertierung", "spezifisch", "freischnitt", "einheit",
            "regenerationsbetrieb", "notfahrbetrieb", "fahrpedalgradient",
            "transitionsfunktion", "unplausibilitaet", "aufhebung",
            "steuergeraet", "gueltigkeit", "verschleiss", "grossignal",
            "ruecksetz", "rueckfuehrung",
            "fahrpedal", "luftmassenmesser", "fahrgeschwindigkeitsgeber",
            "getriebedrehzahl", "rauchbegrenzung", "ladedruckschlauch",
            "ladedruck", "steuerungslogik", "wegfahrsperre", "lernverfahren",
            "nockenwellendrehzahl", "kurbelwellendrehzahl", "luftaufwand",
            "haupteinspritzkorrektur", "startbasismoment", "motorkennlinie",
            "mengenkoordination", "gangwechsel", "abgleichwerte",
            "motormechanikschutz", "mechanikschutz", "bauteileschutz",
            "bauteilschutz", "welle", "nockenwelle", "kurbelwelle"}
_STRONG_HINT = _re.compile(
    "[äöüß]|" + "|".join(sorted((_re.escape(k) for k in _STRONG),
                                key=len, reverse=True)), _re.I)

# Same test, but on already-lowercased text with no IGNORECASE flag: the
# case-insensitive alternation is ~5x slower, and this gate runs on every
# description of every parameter each time the tree is built.
_STRONG_HINT_LC = _re.compile(
    "[äöüß]|" + "|".join(sorted((_re.escape(k.lower()) for k in _STRONG),
                                key=len, reverse=True)))


# One alternation, longest-first, so each position is translated exactly once
# (a sequential pass would re-match a shorter root inside an already-English
# result, e.g. "temperatur" inside "temperature").
_DE_RE = _re.compile("|".join(_re.escape(k) for k, _ in _DE_ORDERED), _re.I)


def _tr_word(w: str) -> str:
    out = _DE_RE.sub(lambda m: " " + _DE_TERMS[m.group(0).lower()] + " ", w)
    out = _re.sub(r"\s+[sn]\s+", " ", out)     # drop German linking s/n
    return _re.sub(r"\s+", " ", out).strip()


# Common German function words, translated only after the description is known
# to be German (whole-word, so English text elsewhere is unaffected).
_DE_FUNC = {
    "der": "the", "die": "the", "das": "the", "den": "the", "des": "of the",
    "dem": "the", "und": "and", "oder": "or", "für": "for", "bei": "at",
    "zum": "for", "zur": "for", "von": "of", "vom": "of the", "im": "in",
    "in": "in", "mit": "with", "auf": "on", "aus": "from", "nach": "after",
    "vor": "before", "ohne": "without", "wenn": "if", "wird": "is",
    "werden": "are", "zwischen": "between", "durch": "via", "als": "as",
    "ein": "a", "eine": "a", "einer": "a", "eines": "a", "ist": "is",
    "sowie": "and", "bzw": "or", "gemäß": "per", "über": "over",
    "nicht": "not", "kein": "no", "keine": "no", "keiner": "no",
    "je": "per", "pro": "per", "diese": "this", "dieser": "this",
    "dieses": "this", "sein": "its", "seiner": "its", "am": "at the",
    "beim": "at the", "zu": "to", "so": "so", "dann": "then",
    "nur": "only", "jedoch": "however", "um": "to",
}
# ASCII-transliterated function words (fuer, ueber, gemaess) for the hq family.
_DE_FUNC.update({_ascii_umlaut(_k): _v for _k, _v in list(_DE_FUNC.items())
                 if _ascii_umlaut(_k) != _k})
_DE_FUNC_RE = _re.compile(
    r"\b(" + "|".join(sorted((_re.escape(k) for k in _DE_FUNC),
                             key=len, reverse=True)) + r")\b", _re.I)


_PHRASES = None


def _phrases() -> dict:
    """Whole-description translations (``data/de_en_phrases.json.gz``): the
    curated English of every Bosch EDC15 DAMOS description the app knows,
    keyed by the German with its spacing collapsed. Loaded on first use."""
    global _PHRASES
    if _PHRASES is None:
        import gzip
        import json
        import os
        path = os.path.join(os.path.dirname(__file__), "data",
                            "de_en_phrases.json.gz")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                _PHRASES = json.load(fh)
        except (OSError, ValueError):
            _PHRASES = {}
    return _PHRASES


#: what a phrase-table template stands in for: an identifier (underscore
#: names, code words with digits), a hex value or a number. A key such as
#: "Fehlerklasse fuer Fehlerpfad {0}" covers every fault path.
_TEMPLATE_ID = _re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_\[\]\.]*|[A-Z]{2,}[0-9]+[A-Z0-9]*"
    r"|0x[0-9A-Fa-f]+|\d+(?:[.,]\d+)?)\b")


def _template(text: str):
    ids = []

    def sub(m):
        ids.append(m.group(0))
        return "{%d}" % (len(ids) - 1)
    return _TEMPLATE_ID.sub(sub, text), ids


def phrase_english(text: str):
    """The curated English of a whole German description, or None -- an
    exact entry, or a template entry with the identifiers and numbers put
    back in."""
    if not text:
        return None
    t = " ".join(text.split())
    table = _phrases()
    en = table.get(t)
    if en is not None:
        return en
    tmpl, ids = _template(t)
    if not ids:
        return None
    en = table.get(tmpl)
    if en is None:
        return None
    return _re.sub(r"\{(\d+)\}",
                   lambda m: ids[int(m.group(1))] if int(m.group(1)) < len(ids)
                   else m.group(0), en)


_WORDS = None
_SHARED = None
_FOLDED = None


def _load_gz(name, empty):
    import gzip
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "data", name)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return empty


def _words() -> dict:
    """Single German words in English (``data/de_en_words.json.gz``); an
    empty value marks a word that is not German and stays as it is."""
    global _WORDS
    if _WORDS is None:
        _WORDS = _load_gz("de_en_words.json.gz", {})
    return _WORDS


def _shared() -> frozenset:
    """German words that are also everyday English words ("die", "also",
    "not", "fast", "will"): translated only inside a German text, never in
    an English one (``data/de_en_shared.json.gz``)."""
    global _SHARED
    if _SHARED is None:
        _SHARED = frozenset(_load_gz("de_en_shared.json.gz", []))
    return _SHARED


def _folded() -> dict:
    """The word table's umlaut words by their ASCII spelling ("uebernahme"
    -> "übernahme"), for definitions that write ue / ae / oe / ss."""
    global _FOLDED
    if _FOLDED is None:
        folded = {}
        for k in _words():
            a = _ascii_umlaut(k)
            if a != k:
                folded.setdefault(a, k)
        _FOLDED = folded
    return _FOLDED


#: German inflection endings, longest first: "multiplikative" -> multiplikativ,
#: "schaltungen" -> schaltung, "inneren" -> inner(e)
_ENDINGS = ("ern", "ens", "es", "en", "er", "em", "ns", "e", "n", "s")

_WORD_RE = _re.compile(r"([A-Za-zÄÖÜäöüß]+)")
_UMLAUT_CH = _re.compile("[äöüß]")


def _plain(lw: str, words: dict):
    """The word table's English for ``lw`` as written or spelt with ue / ae /
    oe / ss for the umlaut; None when neither is in the table."""
    en = words.get(lw)
    if en is None:
        k = _folded().get(lw)
        if k is not None:
            en = words.get(k)
    if en is None and _UMLAUT_CH.search(lw):
        # the table has it in its ASCII spelling ("oelpumpe"), the text with
        # the umlaut ("Ölpumpe")
        en = words.get(_ascii_umlaut(lw))
    return en


@_lru_cache(maxsize=131072)
def _lookup(lw: str):
    """The English of one lower-case German word: the word table (as written
    or with ue/ae/oe/ss for the umlaut), a compound of known words, or an
    inflected form of a known word. "" for a word the table marks as not
    German, None for a word nothing knows."""
    words = _words()
    en = _plain(lw, words)
    if en is not None:
        return en
    en = _compound(lw, words)
    if en:
        return en
    return _inflected(lw, words)


_PARTICIPLE = ("enden", "ender", "endes", "endem", "ende", "end")


def _inflected(lw: str, words: dict, compound: bool = True):
    """``lw`` without a German inflection ending, when that is a known German
    word ("linearer" -> linear, "Schaltungen" -> Schaltung); a present
    participle as the -ing form of its verb ("begrenzenden" -> limiting)."""
    for end in _PARTICIPLE:
        if lw.endswith(end) and len(lw) - len(end) >= 4:
            v = _plain(lw[:-len(end)] + "en", words)
            if v:
                return _gerund(v)
            break
    for end in _ENDINGS:
        base = lw[:-len(end)]
        if len(base) < 4 or not lw.endswith(end):
            continue
        en = _plain(base, words)
        if not en and compound:
            en = _compound(base, words)
        if en:
            if end == "s" and en.lower() == base:
                return None     # an English plural ("definitions"), not German
            return en
    return None


def _german_words(text: str) -> int:
    """How many words of ``text`` the word table knows as German."""
    return _word_counts(text)[0]


def _word_counts(text: str):
    """(German words, other words of three letters or more) in ``text``.
    Identifiers ("Dfp_AFSCD", "MOT7", "jAdaAglex") are neither."""
    n = other = 0
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        if _identifier(text, m.start(), m.end()):
            continue
        lw = w.lower()
        en = _lookup(lw)
        if en and en != lw:
            n += 1
        elif len(lw) >= 3:
            other += 1
    return n, other


def _identifier(text: str, a: int, b: int) -> bool:
    """Whether the letters text[a:b] belong to a code word: touching an
    underscore or a digit ("Dfp_ACCCD", "MOT7", "H2503"), or capitals inside
    a word ("jAdaAglex", "CoEng")."""
    if a > 0 and (text[a - 1] == "_" or text[a - 1].isdigit()):
        return True
    if b < len(text) and (text[b] == "_" or text[b].isdigit()):
        return True
    w = text[a:b]
    return bool(_re.search(r"[a-zäöüß][A-ZÄÖÜ]", w))


def _is_german(text: str) -> bool:
    """An umlaut or German-only root; two words the word table knows as
    German; or a text made only of German words ("Motormoment"). One
    ambiguous word among English ones ("moment of inertia") is not."""
    if _STRONG_HINT_LC.search(text.lower()):
        return True
    n, other = _word_counts(text)
    return n >= 2 or (n >= 1 and other == 0)


#: short German words that never occur in English, translated wherever they
#: stand ("Koordinator fuer Luftfilterventil", "Schwere und Dauer")
_DE_SHORT = frozenset({
    "und", "für", "fuer", "mit", "von", "vom", "zur", "zum", "ist", "bei",
    "beim", "oder", "nicht", "sowie", "bzw", "wird", "werden", "nach",
    "über", "ueber", "gemäß", "gemaess", "einer", "eines", "keine", "kein",
    "der", "des", "dem", "das", "auf", "pro", "durch", "wenn", "falls",
    "nur", "dann", "jedoch", "um"})


def _plainly_german(lw: str, en: str) -> bool:
    """A word to translate even inside an English text: German beyond doubt
    -- not also an English word ("also", "not", "fast"), not a short form
    the table merely spells out ("neg" -> negative, "temp" -> temperature)."""
    if lw in _DE_SHORT:
        return True
    if len(lw) < 4 or lw in _shared():
        return False
    full = en.lower().replace(" ", "")
    if full.startswith(lw) and len(full) - len(lw) >= 2:
        return False                # "neg" -> negative: a short form
    if lw in (full + "s", full + "es"):
        return False                # "regenerations": an English plural
    return True


def _beside_german(text, toks, i) -> bool:
    """Whether the word before or after toks[i] is German: a German word
    that is also English ("die", "moment", "not") is read as German only
    there -- "des inneren Kreises" -- not in "is not used"."""
    for j in (i - 1, i + 1):
        if 0 <= j < len(toks):
            m = toks[j]
            if _identifier(text, m.start(), m.end()):
                continue
            lw = m.group(0).lower()
            en = _lookup(lw)
            if lw in _shared() or lw in _DE_SHORT:
                if lw in _DE_SHORT:
                    return True
                continue
            if (en and en != lw) or (en is None and len(lw) >= 3
                                     and _STRONG_HINT_LC.search(lw)):
                return True
    return False


@_lru_cache(maxsize=65536)
def translate_german(text: str) -> str:
    """Best-effort German->English rendering of a description. One the
    phrase table knows is given its curated English. Any other goes word by
    word -- the word table (umlauts written ue/ae/oe/ss too, compounds and
    inflected forms of its words), then the German roots for a word it does
    not know. A plainly German text is translated throughout; in an English
    one (or a half-translated one: "Schwere counter limit for fault path")
    only the words that are German beyond doubt are, so English words such
    as 'temperature', 'message', 'filter' or 'not' are left untouched.
    Code words (identifiers, "MOT7") are never changed."""
    known = phrase_english(text)
    if known is not None:
        return known
    if not text:
        return text
    german = _is_german(text)
    toks = list(_WORD_RE.finditer(text)) if german else ()
    parts = []
    changed = False
    pos = 0
    for i, m in enumerate(toks or _WORD_RE.finditer(text)):
        parts.append(text[pos:m.start()])
        pos = m.end()
        tok = m.group(0)
        if _identifier(text, m.start(), m.end()):
            parts.append(tok)
            continue
        lw = tok.lower()
        if tok in _CAPS and german:
            parts.append(_CAPS[tok])            # "EIN" / "AUS" switch states
            continue
        if tok in _VERBS and german:
            # written small, "ist" / "soll" are verbs ("ist negativ", "soll
            # erreicht werden"); "Ist-" and "Soll-" are actual and setpoint
            parts.append(_VERBS[tok])
            continue
        en = _lookup(lw)
        if not en and lw in _DE_SHORT:
            en = _DE_FUNC.get(lw) or en
        if en and en.lower() == lw:
            parts.append(tok)        # the same word in English: keep as written
            continue
        if not german:
            if en and _plainly_german(lw, en) and not (
                    tok.isupper() and len(tok) <= 5):
                parts.append(_abbrev(text, m, en))
                changed = True
            else:
                parts.append(tok)
            continue
        if en is not None:
            if en and (lw not in _shared() or lw in _DE_SHORT
                       or _beside_german(text, toks, i)):
                parts.append(_abbrev(text, m, en))
            else:
                parts.append(tok)
            continue
        if _STRONG_HINT_LC.search(lw):
            r = _old_translate(tok)
            parts.append(r[:1].lower() + r[1:])
        else:
            parts.append(_DE_FUNC.get(lw, tok))
    parts.append(text[pos:])
    if not german and not changed:
        return text            # nothing German in it: leave English text alone
    out = _re.sub(_DOT + r"\.", "", "".join(parts))
    out = _re.sub(r"\s+", " ", out).strip()
    out = _re.sub(r"\s+([,.;:])", r"\1", out)
    out = _re.sub(r"\ba/c\b", "A/C", out, flags=_re.I)
    out = _re.sub(r"\b(egr|obd|ecu|dpf)\b", lambda m: m.group(1).upper(), out)
    first = text.split(None, 1)[0] if text.split() else ""
    if first and out.startswith(first):
        return out          # first word kept as written ("jAdaAglex_Fktn:")
    return out[:1].upper() + out[1:] if out else out


#: switch states written in capitals ("Anfangswert EIN")
_CAPS = {"EIN": "ON", "AUS": "OFF"}
#: verbs whose capitalised form is a Bosch prefix ("Ist-Drehzahl")
_VERBS = {"ist": "is", "sind": "are", "soll": "should", "sollen": "should"}

#: marks the full stop of an abbreviation that was written out in full
_DOT = "\x00"


def _abbrev(text: str, m, en: str) -> str:
    """``en`` for the word ``m`` of ``text``; a German abbreviation written
    out in full ("pos." -> "positive", "zul." -> "permissible") loses its
    full stop, unless that stop ends the text."""
    b = m.end()
    if (b + 1 < len(text) and text[b] == "." and text[b + 1] in " -/"
            and len(m.group(0)) <= 6 and not en.endswith(".")
            and len(en) > len(m.group(0))):
        return en + _DOT
    return en


#: a short word that opens a compound, as it reads there: "Istgang" is the
#: actual gear, not "is gear". Prefixes whose sense depends on the verb
#: (ein-, auf-, aus-, ab-, an-, zu-) open no compound; such words are in
#: the word table themselves.
_PREFIX = {
    "ist": "actual", "soll": "setpoint", "vor": "pre", "nach": "post",
    "über": "over", "ueber": "over", "unter": "under", "gegen": "counter",
    "mit": "", "ein": "", "auf": "", "aus": "", "ab": "", "an": "",
    "zu": "", "bei": "", "um": "", "von": "", "vom": "", "zum": "",
    "zur": "", "der": "", "die": "", "das": "", "des": "", "den": "",
    "dem": "", "und": "", "oder": "", "für": "", "fuer": "", "im": "",
    "in": "", "am": "", "als": "", "bis": "", "pro": "", "je": "",
}


#: parts of fewer than four letters that are words of their own inside a
#: compound ("Öl|pegel", "Rad|moment", "Aktivierungs|art")
_SHORT_PARTS = {"öl": "oil", "oel": "oil", "gas": "throttle", "rad": "wheel",
                "tür": "door", "tuer": "door", "kat": "catalyst", "hub": "lift",
                "weg": "travel", "zug": "traction", "art": "type", "not": "emergency"}


#: verb stems that begin compounds ("Heizbetrieb" -> heating operation); an
#: explicit list, since a word that merely takes -en ("höchst", "sonder") is
#: no verb
_STEM_PARTS = {
    "heiz": "heating", "aufheiz": "heat-up", "vorheiz": "preheating",
    "nachheiz": "post-heating", "kühl": "cooling", "kuehl": "cooling",
    "mess": "measuring", "spül": "purge", "spuel": "purge", "schalt": "shift",
    "regel": "control", "prüf": "test", "pruef": "test", "steuer": "control",
    "lade": "charge", "saug": "intake", "brenn": "combustion", "dosier": "dosing",
    "schließ": "closing", "schliess": "closing", "lern": "learning", "zähl": "count",
    "zaehl": "count", "stell": "actuating", "misch": "mixing", "wärm": "heating",
    "waerm": "heating", "fahr": "driving", "anfahr": "drive-off",
    "abschalt": "switch-off", "einschalt": "switch-on", "umschalt": "switchover",
    "zuschalt": "engagement", "abstell": "shutdown", "rückstell": "reset",
    "rueckstell": "reset", "einspritz": "injection", "entprell": "debounce",
    "verstell": "adjustment", "halte": "holding", "warte": "waiting",
    "lösch": "delete", "loesch": "delete", "schreib": "write", "lese": "read",
    "such": "search", "sperr": "inhibit",
}


def _gerund(en: str) -> str:
    """The -ing form of an English verb's first word ("heat" -> heating,
    "measure" -> measuring, "switch off" -> switching off)."""
    head, _sp, rest = en.partition(" ")
    if not head.isalpha() or head.endswith("ing"):
        return en
    if head.endswith("e") and not head.endswith(("ee", "ye", "oe")):
        head = head[:-1]
    return head + "ing" + (" " + rest if rest else "")


def _compound(w: str, words: dict, depth: int = 0):
    """A German compound the word table has no entry for, as the words it
    is made of: "fehlerklassentabelle" -> "fault class table",
    "startzuendwinkelrampe" -> "start ignition angle ramp". Every way of
    cutting the word into table words is tried and the one with the fewest
    parts wins (then the longest first part). A linking s / n / en / es may
    follow any part but the last; the parts may be written with ue/ae/oe/ss;
    the first may be a prefix ("Istgang" -> actual gear) or an English word
    the German borrowed ("Signalnummer"), the last an English word
    ("Fehlercode" -> fault code) or an inflected form ("...rampen"). At
    least one part must be plainly German. None when nothing fits.
    (``depth`` is kept for callers; the search is not recursive.)"""
    n = len(w)
    if n < 7:
        return None
    links = ("", "s", "n", "en", "es")
    shared = _shared()

    def piece(a: int, b: int, first: bool, last: bool):
        """(english, german?) for w[a:b] as one part, or None."""
        p = w[a:b]
        if first and (p in _DE_FUNC or p in _PREFIX):
            en = _PREFIX.get(p)
            return (en, True) if en else None
        if p in _DE_FUNC:
            return None              # "allgemein" is not "allgem" + "ein"
        if p in _SHORT_PARTS:
            return _SHORT_PARTS[p], True
        if len(p) < 4:
            return None              # three letters: "war", "der" -- too many false cuts
        en = _plain(p, words)
        if en is None and not last and p in _STEM_PARTS:
            return _STEM_PARTS[p], True  # "Heiz|betrieb", "Kühl|stufe"
        if en:
            return en, en.lower() != p and p not in shared
        if en == "" and len(p) >= 4 and (first or last):
            return p, False          # an English word the German borrowed
        if last and len(p) >= 6:
            en = _inflected(p, words, compound=False)    # "seite" is not "seit" + e
            if en:
                return en, True
        return None

    # best[i]: the best way to cut w[:i] -- (parts, -len(first), [(en, de?)])
    best = {0: (0, 0, [])}
    for i in range(n):
        if i not in best:
            continue
        parts, firstlen, seq = best[i]
        if parts >= 5:
            continue
        for j in range(i + 2, n + 1):
            last = j == n
            got = piece(i, j, i == 0, last)
            if got is None:
                continue
            if last:
                ends = [(j, "")]
            else:
                ends = [(j + len(lk), lk) for lk in links
                        if w.startswith(lk, j) and j + len(lk) < n]
            for k, lk in ends:
                if got[1] is False and i > 0 and not last:
                    continue             # English only at either end
                if last and got[1] is False and i > 0 and (
                        len(w) - i == n or seq and seq[-1][2]):
                    continue             # borrowed head needs no link before it
                cand = (parts + 1, -j if i == 0 else firstlen, seq + [(got[0], got[1], lk)])
                old = best.get(k)
                if old is None or cand[:2] < old[:2]:
                    best[k] = cand
    got = best.get(n)
    if not got or got[0] < 2 or not any(de for _en, de, _lk in got[2]):
        return None
    seq = got[2]
    if seq[0][1] is False and not any(de for _en, de, _lk in seq[1:]):
        return None
    return " ".join(en for en, _de, _lk in seq if en)


def _old_translate(text: str) -> str:
    """The German-root rendering of a word the word table does not know."""
    # A strong marker means the description is German, so translate the whole
    # text: every known root (single pass, longest-first) and function word.
    # English descriptions never reach here, so their words are never rewritten.
    joined = _DE_RE.sub(lambda m: " " + _DE_TERMS[m.group(0).lower()] + " ", text)
    # drop a lone German linking/inflection letter left beside a translated root
    # (linking s/n, and adjective endings e/r that survive "abhaengige" ->
    # "dependent e"); done before/after collapsing so mid- and end-of-word hit.
    joined = _re.sub(r"\s+(?:[esnr]|en|er|es|em|ns)\s+", " ", joined)
    joined = _re.sub(r"\s+(?:[esnr]|en|er|es|em|ns)\s*$", "", joined)
    joined = _DE_FUNC_RE.sub(
        lambda m: _DE_FUNC[m.group(0).lower()], joined)
    out = _re.sub(r"\s+", " ", joined).strip()
    out = _re.sub(r"\s+([,.;:])", r"\1", out)
    out = _re.sub(r"\ba/c\b", "A/C", out, flags=_re.I)
    return out[:1].upper() + out[1:] if out else out


# --- curated: exact A2L symbol -> plain-English name -------------------------
CURATED = {
    # Engine speed / position / load / torque
    "Eng_nAvrg": "Engine Speed",
    "CaSCD_nPeriod": "Camshaft Sensor Speed",
    "LIGov_nSetpoint": "Idle Speed Setpoint",
    "EngM_stSync": "Crank/Cam Sync Status",
    "CoEng_stEng": "Engine State",
    "CoEng_rTrq": "Engine Load",
    "CoEng_trqInrLim": "Inner Torque Limit",
    "FMTC_trqInr": "Inner Torque (actual)",
    "FMTC_trqInrLimSmk": "Smoke-Limit Torque",
    "EngPrt_trqLim": "Engine Protection Torque Limit",
    "AccPed_trqDes": "Driver Demand Torque",
    "AccPed_trqInrDesSig": "Driver Demand (inner torque)",
    "CoEng_dvolFlConsum": "Fuel Consumption Rate",
    # Injection
    "InjCtl_qCurr": "Injection Quantity",
    "InjCtl_qSetUnBal": "Injection Quantity (setpoint)",
    "FMTC_qStrt": "Injection Quantity (start)",
    "InjCrv_phiMI1Des": "Main Injection Timing (desired)",
    "InjVlv_phiMI1DurCorr": "Main Injection Duration",
    "FBC_qDvtCyl": "Cylinder Balance Correction",
    "InjVlv_nCyl1": "Cylinder 1 Speed",
    "InjVlv_nCyl2": "Cylinder 2 Speed",
    "InjVlv_nCyl3": "Cylinder 3 Speed",
    "InjVlv_nCyl4": "Cylinder 4 Speed",
    "InjVlv_nCyl5": "Cylinder 5 Speed",
    "InjVlv_nCyl6": "Cylinder 6 Speed",
    "InjVlv_qFBCCyl1": "Cylinder 1 Balance Correction",
    "InjVlv_qFBCCyl2": "Cylinder 2 Balance Correction",
    "InjVlv_qFBCCyl3": "Cylinder 3 Balance Correction",
    "InjVlv_qFBCCyl4": "Cylinder 4 Balance Correction",
    "InjVlv_qFBCCyl5": "Cylinder 5 Balance Correction",
    "InjVlv_qFBCCyl6": "Cylinder 6 Balance Correction",
    # Coolant / temps
    "CTSCD_tClnt": "Coolant Temperature",
    "CTSCD_tClntLin": "Coolant Temperature (linearized)",
    "CTSCD_uRaw": "Coolant Temp Voltage",
    "COTSCD_tClnt": "Coolant Temperature (radiator outlet)",
    "CtTCtl_tClntDes": "Coolant Temp Setpoint",
    "IATSCD_tAir": "Intake Air Temperature",
    "IATSCD_tAirLin": "Intake Air Temperature (linearized)",
    "FTSCD_tFuel": "Fuel Temperature",
    "OTSCD_tEngOil": "Engine Oil Temperature",
    "EGPpCD_tPreTrbn": "Exhaust Temp (pre-turbo)",
    "EGSys_tEGLSU": "Exhaust Gas Temperature (Lambda)",
    "PFltCD_tPre": "DPF Inlet Temperature",
    "PFlt_tOxiPst": "Post-DOC Temperature",
    "PFlt_tOxiPre": "Pre-DOC Temperature",
    "PFlt_tPst": "DPF Outlet Temperature",
    "PFlt_tPre": "DPF Inlet Temperature",
    # Air / MAF / boost / EGR
    "AFSCD_dmAirPerTime": "Air Mass Flow (MAF)",
    "AFSCD_dmAirRawPerTime": "Air Mass Flow (MAF, raw)",
    "AFSCD_dmAirNorm": "Air Mass Flow (normalized)",
    "AFSCD_mAirPerCyl": "Air Mass per Cylinder",
    "AFSCD_mAirPerCylB1": "Air Mass per Cylinder (bank 1)",
    "AFSCD_rAirNorm": "Air Mass Ratio (normalized/sensor)",
    "AFSCD_uRaw": "MAF Sensor Voltage",
    "AFSCD_stPlSetyDrft_mp": "MAF Drift Check Status",
    "AirCtl_mDesVal": "Air Mass Setpoint",
    "AirCtl_mGovDev": "Air Mass Control Deviation",
    "AirCtl_rEGR": "EGR Actuator Duty",
    "EGRCD_rOut": "EGR Valve Duty",
    "EGRSCD_rRelPos": "EGR Valve Position",
    "EGRVlv_rGvnrDvtRel": "EGR Control Deviation",
    "BPSCD_pOutVal": "Boost Pressure",
    "BPSCD_pFltVal": "Boost Pressure (filtered)",
    "BPSCD_pLin": "Boost Pressure (linearized)",
    "BPSCD_pAvgDiff": "Boost/Ambient Pressure Difference",
    "BPACD_rOut": "Boost Actuator Duty",
    "BPACD_rOut2": "Boost Actuator Duty 2",
    "BPASCD_rRelPos": "Boost Actuator Position",
    "BPASCD_stOfs_mp": "Boost Actuator Offset Adapt Status",
    "PCR_pBDes": "Boost Pressure Setpoint",
    "PCR_rBPACD": "Boost Actuator Command",
    "PCR_rBalGvnrDiff": "Boost Balance Duty",
    "PCR_stPCR_mp": "Boost Control Status",
    "ASMod_pIntMnfS16": "Intake Manifold Pressure",
    "ASMod_pTurbPreS16": "Pre-Turbine Pressure",
    "APSCD_pVal": "Ambient Pressure",
    "APSCD_pSensorVal": "Ambient Pressure (sensor)",
    "APSCD_uRaw": "Ambient Pressure Voltage",
    "TVACD_rOut": "Throttle Valve Duty",
    "TVACD_stLine": "Throttle Status Line",
    # Pedal / cruise / vehicle
    "APPCD_rAPP1UnFlt": "Accelerator Pedal 1 (unfiltered)",
    "APPCD_rFlt": "Accelerator Pedal (filtered)",
    "APPCD_stSwitchSig": "Pedal Switch Status",
    "APPCD_uRawAPP1": "Accelerator Pedal 1 Voltage",
    "APPCD_uRawAPP2": "Accelerator Pedal 2 Voltage",
    "VSSCD_v": "Vehicle Speed",
    "VSSCD_vCarb1": "Vehicle Speed (OBD)",
    "VSSCD_a": "Vehicle Acceleration",
    "CrCtl_trqDes": "Cruise Torque Request",
    "CrCtl_stKeyStateSig": "Cruise Control Keys & State",
    "CrCtl_stShutOff": "Cruise Shutoff Conditions",
    "CrCtl_stShutOffrev": "Cruise Shutoff (reversible)",
    "CrCCD_stKey": "Cruise Key Pressed",
    "CrCCD_stKeySig": "Cruise Control Keys",
    "Gearbx_stGear": "Gear",
    "Gearbx_trqDec": "Gearbox Torque Reduction",
    # Traction / stability torque interventions
    "CoVM_stActDem": "Active Torque Interventions",
    "CoVM_trqDec": "ASR Torque Reduction",
    "CoVM_trqInc": "MSR Torque Increase",
    "FrmMng_stDCS": "MSR Intervention Status",
    "FrmMng_stTCS": "ASR Intervention Status",
    "FrmMng_stChrgr1Err1": "Turbo 1 Fault 1",
    "FrmMng_stChrgr1Err2": "Turbo 1 Fault 2",
    "FrmMng_stChrgr2Err1": "Turbo 2 Fault 1",
    "FrmMng_stChrgr2Err2": "Turbo 2 Fault 2",
    "FrmMng_lOil_mp": "Oil Level Threshold",
    # Electrical / accessories
    "BattCD_u": "Battery Voltage",
    "AltCD_rAltLoad": "Alternator Load",
    "Alt_pwrDes": "Alternator Power Demand",
    "SrvPmp_pwrDes": "Steering Pump Power Demand",
    "FanCtl_dcycFan1": "Cooling Fan 1 Duty",
    "FanCtl_dcycFan2": "Cooling Fan 2 Duty",
    "GlwCtl_stLogic": "Glow Plug State",
    "GlwCtl_tiPre": "Preglow Time",
    "StSysCtl_stT50CDOn": "Starter Relay Status",
    "PSP_stLogicOut": "Pre-Supply Pump Status",
    "FlSys_stFlPp": "Fuel Pump Status",
    "ACCD_trqCompr": "A/C Compressor Torque",
    "ACCD_stMnSwtRawVal": "A/C Main Switch (raw)",
    "ACCtl_stBitStructure": "A/C Shutoff Conditions",
    "ACCtl_stLowByte_mp": "A/C Shutoff Conditions (low byte)",
    "ACCtl_stLogicOut": "A/C Compressor Command",
    "ACCCD_stBrkErr_mp": "ACC Sensor Fault",
    "UAcc_stFuncCodePresent": "Function Coding Present",
    "UAcc_swtFuncCodeAC": "A/C Function Code",
    "ImmCtl_tiLockCount": "Immobilizer Lock Time",
    # Lambda / exhaust aftertreatment
    "LSU_rLmbd": "Lambda",
    "LSU_rO2MaxVal": "Lambda O2 Upper Limit",
    "LSUCD_stAdpt": "Lambda Adaptation Status",
    "LSUCD_resRiClb": "Lambda Ri Offset Correction",
    "LSUCD_uO2Clb": "Lambda O2 Offset Correction",
    "LSUCD_uRiRaw": "Lambda Ri Voltage",
    "LSUCD_mAirAftOvrrun": "Air Mass Since Overrun",
    "LSUCD_mAirAftOvrRun_mp": "Air Mass Since Overrun",
    "EGT_st": "Regeneration Request",
    "PFltCD_pDiff": "DPF Differential Pressure",
    "PFlt_numEngPOp_mp": "DPF Regen Measure",
    "PFlt_qFl_mp": "Fuel Since Regen",
    "SvLp_dOilLoad": "Oil Load",
    "SvLp_dSootLoad": "Soot Load",
    "AOHtCD_volFlConsum": "Aux Heater Fuel Use",
    "AOHt_NumRly_mp": "Aux Heater Active Elements",
    "AOHt_stHt2Out": "Aux Heater 2 Command",
    "AOHt_stShutOff_mp": "Aux Heater Shutoff Conditions",
    "AddPCD_numStrkRem_mp": "Additive Pump Strokes Left",
    # Diagnostics / readiness / OBD
    "DSM_ctFcmEntry": "Stored Fault Count",
    "DSM_ctRdyActCyc_mp": "Readiness Bits (current cycle)",
    "DSM_stRdyAB": "OBD Readiness (bytes A/B)",
    "DSM_stRdyCD": "OBD Readiness (bytes C/D)",
    "CARB_Mode1PID1D": "OBD O2 Sensor Config (PID 1D)",
    "CARB_Mode1PID4F": "OBD Lambda Config (PID 4F, bytes 1-4)",
    "CARB_Mode1PID4FB": "OBD Lambda Config (PID 4F, bytes 5-6)",
    "CARB_TstNotUsedFalse": "OBD Mode 6 Unused Limit (bool)",
    "CARB_TstNotUsedMinS": "OBD Mode 6 Unused Limit (signed)",
    "CARB_stRdyCurrABCD": "OBD Readiness (current cycle)",
    "NetMng_stCAN": "CAN Bus Status",
    "HWEMon_numRecovery": "Last Reset Source",
    "EEPCD_stOptErrCodSet": "EEPROM Option Error Code",
    "FMA_stSwtOff": "Fuel Metering Shutoff Status",
    "TstTog_stTstEnd": "Test End Status",
    "Tst_Zyklflag_SB1": "EOL Cycle Flag 1",
    "Tst_Zyklflag_SB2": "EOL Cycle Flag 2",
    "Tst_Zyklflag_SB3": "EOL Cycle Flag 3",
    "Tst_Zyklflag_SB4": "EOL Cycle Flag 4",
    "Tst_Zyklflag_SB5": "EOL Cycle Flag 5",
    "Tst_Zyklflag_SB6": "EOL Cycle Flag 6",
    "Tst_Zyklflag_SB7": "EOL Cycle Flag 7",
    "PrpCtl_stSPTrack_mp": "Setpoint Tracking Status",
    "PrpCtl_vCurrTrack_mp": "Tracked Speed Setpoint",
}

# Diagnostic fault-path monitors (Dfp_*): named generically as faults.
_DFP = {
    "Dfp_HWEMonCom_Low": "Fault: HW Monitor Comms",
    "Dfp_HWEMonEEPROM_Low": "Fault: HW Monitor EEPROM",
    "Dfp_HWEMonUMinSupply_Low": "Fault: Supply Voltage Low",
    "Dfp_HWEMonUMaxSupply_Low": "Fault: Supply Voltage High",
    "Dfp_LSUCDCircNernst0_Low": "Fault: Lambda Nernst Circuit",
    "Dfp_LSUCDCircPumpCur0_Low": "Fault: Lambda Pump Current Circuit",
    "Dfp_LSUCDCircVirtGnd0_Low": "Fault: Lambda Virtual Ground",
    "Dfp_LSUCDHeater0_Low": "Fault: Lambda Heater",
    "Dfp_LSUCDHtCoup0_Low": "Fault: Lambda Heater Coupling",
    "Dfp_LSUCDO20_Low": "Fault: Lambda O2 Signal",
    "Dfp_LSUCDO2Clb0_Low": "Fault: Lambda O2 Calibration",
    "Dfp_LSUCDRiClb0_Low": "Fault: Lambda Ri Calibration",
    "Dfp_LSUCDSPI0_Low": "Fault: Lambda SPI",
    "Dfp_LSUCDWireIP0_Low": "Fault: Lambda IP Wire",
    "Dfp_LSUCtlRiExc0_Low": "Fault: Lambda Ri Excitation",
    "Dfp_LSUMon1Sens_Low": "Fault: Lambda Monitor Sensor 1",
    "Dfp_LSUMon2Sens_Low": "Fault: Lambda Monitor Sensor 2",
    "Dfp_LSUMonFullLd_Low": "Fault: Lambda Monitor Full Load",
    "Dfp_LSUMonPartLd_Low": "Fault: Lambda Monitor Part Load",
    "Dfp_SOPTst_Low": "Fault: Start-of-Production Test",
}
CURATED.update(_DFP)

# --- systematic fallback -----------------------------------------------------
_MODULE = {
    "Eng": "Engine", "EngM": "Engine Position", "EngPrt": "Engine Protection",
    "CoEng": "Engine", "FMTC": "Torque", "FMA": "Fuel Metering",
    "CTSCD": "Coolant", "COTSCD": "Coolant", "CtTCtl": "Coolant",
    "IATSCD": "Intake Air", "EATSCD": "Intake Air", "FTSCD": "Fuel",
    "OTSCD": "Oil", "FTTSCD": "Fuel",
    "AFSCD": "Air Mass", "AirCtl": "Air System", "ASMod": "Air System",
    "BPSCD": "Boost", "BPACD": "Boost Actuator", "BPASCD": "Boost Actuator",
    "APSCD": "Ambient Pressure", "PCR": "Boost Control",
    "EGRCD": "EGR", "EGRSCD": "EGR", "EGRVlv": "EGR",
    "TVACD": "Throttle", "APPCD": "Accelerator Pedal", "AccPed": "Accelerator Pedal",
    "VSSCD": "Vehicle", "CrCtl": "Cruise", "CrCCD": "Cruise",
    "InjCtl": "Injection", "InjVlv": "Injector", "InjCrv": "Injection Timing",
    "FBC": "Cylinder Balance", "PCRV": "Rail Pressure",
    "BattCD": "Battery", "AltCD": "Alternator", "Alt": "Alternator",
    "GlwCtl": "Glow Plug", "FanCtl": "Cooling Fan", "SrvPmp": "Steering Pump",
    "ACCD": "A/C", "ACCtl": "A/C", "ACCCD": "ACC",
    "LSU": "Lambda", "LSUCD": "Lambda",
    "PFlt": "DPF", "PFltCD": "DPF", "EGPpCD": "Exhaust", "EGPp": "Exhaust",
    "EGSys": "Exhaust", "EGT": "Aftertreatment", "OxiCCD": "Oxidation Cat",
    "SvLp": "Service Load", "AOHt": "Aux Heater", "AOHtCD": "Aux Heater",
    "AddPCD": "Additive Pump", "PSP": "Fuel Pump", "FlSys": "Fuel System",
    "DSM": "Diagnostics", "Dfp": "Fault", "CARB": "OBD", "NetMng": "Network",
    "FrmMng": "Coordinator", "CoVM": "Vehicle Coordinator", "Gearbx": "Gearbox",
    "HWEMon": "HW Monitor", "EEPCD": "EEPROM", "UAcc": "Coding", "UKey": "Coding",
    "ImmCtl": "Immobilizer", "LIGov": "Idle Governor", "CaSCD": "Camshaft",
    "Tst": "Test", "TstTog": "Test", "PrpCtl": "Speed Control",
    "StSysCtl": "Start System",
}
_QTY = {
    "n": "Speed", "t": "Temperature", "p": "Pressure", "r": "Rate",
    "m": "Mass", "dm": "Mass Flow", "q": "Quantity", "u": "Voltage",
    "v": "Speed", "a": "Acceleration", "I": "Current", "phi": "Angle",
    "ti": "Time", "st": "Status", "ct": "Count", "num": "Count",
    "trq": "Torque", "pwr": "Power", "dcyc": "Duty", "vol": "Volume",
    "dvol": "Flow", "l": "Level", "fac": "Factor", "res": "Value",
    "swt": "Code", "d": "Load", "pos": "Position",
}
_TOKENS = [
    ("UnFlt", "unfiltered"), ("Flt", "filtered"), ("Raw", "raw"),
    ("Lin", "linearized"), ("Avrg", "average"), ("Des", "desired"),
    ("SetUn", "setpoint"), ("Set", "setpoint"), ("Nom", "nominal"),
    ("Cor", "corrected"), ("Corr", "corrected"), ("Ofs", "offset"),
    ("Rel", "relative"), ("Abs", "absolute"), ("Min", "min"), ("Max", "max"),
    ("Lim", "limit"), ("Pre", "pre"), ("Pst", "post"), ("Dev", "deviation"),
]

def _systematic(sym):
    base = sym.split("_")[0]
    module = _MODULE.get(base, base)
    rest = sym[len(base)+1:] if "_" in sym else ""
    rest = rest[:-3] if rest.endswith("_mp") else rest
    qty = ""
    i = 0
    while i < len(rest) and (rest[i].islower() or (rest[i] == "I" and i == 0)):
        i += 1
    code = rest[:i]
    qty = _QTY.get(code, "")
    quals = [w for tok, w in _TOKENS if tok in rest]
    parts = [module]
    if qty:
        parts.append(qty)
    name = " ".join(parts)
    if quals:
        name += " (" + ", ".join(dict.fromkeys(quals)) + ")"
    return name

# --- fault-path (Dfp_*) naming ----------------------------------------------
# Module prefixes as they read in a fault context (a superset of _MODULE tuned
# for monitors: many DSM paths start with a subsystem code + a monitor suffix).
_FAULT_MODULE = {
    "AFSCD": "Mass Air Flow Sensor", "AFS": "Mass Air Flow Sensor",
    "APPCD": "Accelerator Pedal Sensor", "APP": "Accelerator Pedal",
    "APSCD": "Ambient Pressure Sensor", "BPSCD": "Boost Pressure Sensor",
    "BPACD": "Boost Actuator", "PCR": "Boost Pressure Control",
    "CTSCD": "Coolant Temp Sensor", "COTSCD": "Radiator Outlet Temp Sensor",
    "IATSCD": "Intake Air Temp Sensor", "EATSCD": "Intake Air Temp Sensor",
    "FTSCD": "Fuel Temp Sensor", "OTSCD": "Oil Temp Sensor",
    "EGRCD": "EGR", "EGRSCD": "EGR Position Sensor", "EGRVlv": "EGR Valve",
    "TVACD": "Throttle Valve", "VSSCD": "Vehicle Speed", "VSACD": "Vehicle Accel",
    "CrCtl": "Cruise Control", "CrCCD": "Cruise Control",
    "InjVlv": "Injector", "InjCrv": "Injection Timing", "InjCtl": "Injection",
    "PCRV": "Rail Pressure Valve", "RailCD": "Rail Pressure Sensor",
    "FBC": "Cylinder Balance", "BattCD": "Battery", "AltCD": "Alternator",
    "GlwCtl": "Glow Plug", "FanCtl": "Cooling Fan", "SrvPmp": "Steering Pump",
    "ACCD": "A/C Compressor", "ACCCD": "Adaptive Cruise (ACC)",
    "LSUCD": "Wideband Lambda", "LSU": "Lambda", "LSUMon": "Lambda Monitor",
    "PFltCD": "DPF Pressure Sensor", "PFlt": "DPF", "EGPpCD": "Exhaust Pressure",
    "EGSys": "Exhaust", "EGT": "Aftertreatment Temp", "OxiCCD": "Oxidation Cat",
    "AOHtCD": "Aux Heater", "AddPCD": "Additive Pump", "PSP": "Fuel Pump",
    "HWEMon": "ECU Hardware", "EEPCD": "EEPROM", "TPUMon": "TPU",
    "FrmMng": "CAN Coordinator", "NetMng": "CAN Network", "WdCom": "Watchdog",
    "Gearbx": "Gearbox", "CaSCD": "Camshaft Sensor", "OSC": "Crankshaft Sensor",
    "LIGov": "Idle Governor", "StrtCtl": "Start System", "SOP": "Production Test",
    "Montr": "Monitoring", "TVA": "Throttle", "PrpCtl": "Speed Limiter",
    "USAp": "Coding/Dataset", "UAcc": "Coding", "UKey": "Immobilizer",
    "ThrVlv": "Throttle Valve", "PrhCtl": "Preheat Control",
}
# Monitor / fault suffixes (order matters: longer first).
_FAULT_SUFFIX = [
    ("WrongCoded", "wrongly coded"), ("SRCMax", "circuit high"),
    ("SRCMin", "circuit low"), ("SRCGnd", "short to ground"),
    ("SRCBatt", "short to battery"), ("SRCVbat", "short to battery"),
    ("SRC", "circuit range/performance"), ("_High", "high"), ("_Low", "low"),
    ("_PAC", "(compressor active)"), ("CircPumpCur", "pump-current circuit"),
    ("CircNernst", "Nernst circuit"), ("CircVirtGnd", "virtual-ground circuit"),
    ("HtCoup", "heater coupling"), ("Htr", "heater"), ("Ht", "heater"),
    ("OpenLoad", "open circuit"), ("OL", "open circuit"), ("OpLd", "open circuit"),
    ("ShrtCir", "short circuit"), ("Short", "short circuit"),
    ("Plaus", "implausible signal"), ("Npl", "implausible signal"),
    ("NPL", "implausible signal"), ("Sig", "signal fault"),
    ("Drft", "signal drift"), ("Drift", "signal drift"),
    ("Sty", "safety check"), ("Sety", "safety check"), ("Mon", "monitoring"),
    ("Inac", "inactive/no message"), ("Inact", "inactive/no message"),
    ("CanSig", "CAN signal fault"), ("CanInac", "CAN timeout"),
    ("CAN", "CAN fault"), ("TO", "timeout"), ("Timeout", "timeout"),
    ("BrkErr", "brake-signal fault"), ("Brk", "brake signal"),
    ("SensErr", "sensor fault"), ("Sens", "sensor"), ("Circ", "circuit"),
    ("Sync", "sync fault"), ("MaxLim", "above limit"), ("MinLim", "below limit"),
    ("Max", "high"), ("Min", "low"), ("Def", "defect"), ("Err", "fault"),
]


def _split_camel(s):
    import re as _re
    return _re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+\d*|[A-Z]+\d*|\d+", s)


def fault_name(dfp):
    """Plain-English fault description from a ``Dfp_<name>`` / ``<name>`` symbol,
    e.g. 'AFSCD_SRCAirm' -> 'Mass Air Flow Sensor circuit range/performance'."""
    if not dfp:
        return ""
    name = dfp[4:] if dfp.startswith("Dfp_") else dfp
    if name.endswith("_C"):
        name = name[:-2]
    if ("Dfp_" + name) in CURATED:
        return CURATED["Dfp_" + name]
    # module prefix (longest match wins)
    base = name.split("_")[0]
    mod = None
    for k in sorted(_FAULT_MODULE, key=len, reverse=True):
        if base.startswith(k) or name.startswith(k):
            mod = _FAULT_MODULE[k]
            k_len = len(k)
            break
    # match monitor suffixes longest-token-first, consuming each match so a
    # sub-token (e.g. 'Circ' inside 'CircPumpCur') can't also fire
    quals = []
    residual = name
    for tok, phrase in sorted(_FAULT_SUFFIX, key=lambda t: -len(t[0])):
        if tok in residual:
            if phrase not in quals:
                quals.append(phrase)
            residual = residual.replace(tok, " ")
    if mod:
        label = mod
        if quals:
            label += " " + ", ".join(dict.fromkeys(quals))
        return label
    # no known module: title-case the split symbol
    words = _split_camel(name.replace("_", " "))
    pretty = " ".join(w if w.isupper() else w.capitalize() for w in words)
    if quals:
        pretty += " (" + ", ".join(dict.fromkeys(quals)) + ")"
    return pretty or name


def human_name(symbol):
    """Plain-English name for one A2L symbol (no symbol appended)."""
    if not symbol:
        return ""
    if symbol in CURATED:
        return CURATED[symbol]
    return _systematic(symbol)

def humanize(symbol, unit=None):
    """'Human Name (A2L_symbol)' [+ ' [unit]' if given]."""
    if not symbol:
        return ""
    label = "%s (%s)" % (human_name(symbol), symbol)
    if unit and unit not in ("-", ""):
        label += " [%s]" % unit
    return label


# ------------------------------------------------------------------ units
# German unit words in Bosch definitions (DAMOS /REG, OLS, A2L) -> the
# English the EDC16 definitions use: "mg/Hub Luft" -> "mg/stroke air",
# "Grad KW" -> "deg CrS", "Grad C" -> "deg C".
_UNIT_PHRASES = (
    (r"Grad\s*KW\s+auf\s+2\.\s*statis", "deg CrS (2nd static)"),
    (r"Grad\s*KW", "deg CrS"),
    (r"Grad\s*NW", "deg CaS"),
    (r"Grad\s*C\b", "deg C"),
    (r"U/min", "rpm"),
    (r"Grad", "deg"),
)
_UNIT_WORDS = {
    "luft": "air", "hub": "stroke", "hu": "stroke", "umdrehungen": "rev",
    "umdrehung": "rev", "zyl": "cyl", "zylinder": "cyl", "imp": "pulse",
    "impulse": "pulses", "impuls": "pulse", "diesel": "diesel",
    "zaehne": "teeth", "zahn": "tooth", "zeit": "time", "menge": "qty",
    "spannung": "V", "weg": "dist", "drehzahl": "rpm", "winkel": "deg",
    "druck": "pressure", "temperatur": "temp", "stunden": "h",
}
_UNIT_PHRASE_RE = [(_re.compile(a, _re.I), b) for a, b in _UNIT_PHRASES]
_UNIT_WORD_RE = _re.compile(r"(?<![A-Za-z])([A-Za-z]+)(?![A-Za-z])")


@_lru_cache(maxsize=4096)
def english_units(units: str) -> str:
    """A definition's unit text in English (unchanged when it has no German
    in it): ``mg/Hub`` -> ``mg/stroke``, ``Grad C`` -> ``deg C``, ``1/min``
    stays."""
    if not units:
        return units
    out = units
    for rx, en in _UNIT_PHRASE_RE:
        out = rx.sub(en, out)
    return _UNIT_WORD_RE.sub(
        lambda m: _UNIT_WORDS.get(m.group(1).lower(), m.group(1)), out)


_SC_WORD = _re.compile(r"[A-Za-z]+")
_SC_TITLE = _re.compile(r"[A-Z][a-z]+$")


@_lru_cache(maxsize=8192)
def sentence_case(text: str) -> str:
    """A signal's display name in sentence case: first letter upper, the rest
    of the words lower -- ``engine speed`` and ``Engine Speed`` both become
    ``Engine speed``. Acronyms and identifiers are left as they are (EGR,
    CAN, A/C, MAF, xcwGrp03, InjCtl_qSet): only an all-lowercase first word
    is capitalised, and only a plain Capitalised word after it is lowered."""
    if not text:
        return text
    out, first = [], True
    pos = 0
    for m in _SC_WORD.finditer(text):
        a, b = m.span()
        w = m.group(0)
        # part of an identifier (Eng_nAvrg, xcwGrp03): not a word to case
        ident = ((a > 0 and (text[a - 1] == "_" or text[a - 1].isdigit()))
                 or (b < len(text) and (text[b] == "_" or text[b].isdigit())))
        if first:
            first = False
            if not ident and w.islower():
                w = w[0].upper() + w[1:]
        elif not ident and _SC_TITLE.match(w):
            w = w.lower()
        out.append(text[pos:a])
        out.append(w)
        pos = b
    out.append(text[pos:])
    return "".join(out)

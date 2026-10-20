# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Torque-converter lock-up (ZF "WK") characteristic labels, shared by the
ZF 6HP and ZF 8HP modules.

WHERE THE NAMES COME FROM. Both gearboxes run the same ZF lock-up module (WT).
Its tables are read by the program like this (8K0927158J, functions 0xC35414 /
0xC33D84 / 0xC61780; the ZF A2L ``AUAM14C0_V2`` names every part):

* the characteristic for the current gear is ``WT_KWK_ZTAB[line][gear - 1]``,
  where ``gear`` is the DESTINATION gear (A2L input ``GETR_RGA_DEST``) and the
  line comes from the shift program (``WT_ZUSTAND``) or a fixed override;
* characteristic ``k`` lives in map ``WT_NWK_SK[(k - 1) // 10]`` at column
  ``n * ((k - 1) % 10) + threshold`` -- so a column group is a characteristic
  SLOT and nothing else: its gear exists only through the ZTAB;
* each characteristic holds up to four output-shaft-speed thresholds, named in
  the A2L's own verbal table ``TAB_NKW_SKn_QUAD`` as RO / OR / RC / COC. The
  state machine (open, regulated, closed, overclosed) compares output speed
  with exactly one of them per state:

      open        -> regulated   above OR   (straight to closed when slip
                                             regulation is unavailable)
      regulated   -> open        below RO
      regulated   -> closed      above RC
      closed      -> regulated   below RC - WT_NWK_HYST
      closed      -> overclosed  above COC
      overclosed  -> closed      below COC - WT_NWK2_HYST

  10000 is ZF's "threshold disabled". A three-threshold characteristic (the
  30-column 6HP maps) is RO / OR / RC with COC absent.

Qt-free: the table view, the CLI, an exported definition and the AI tools all
take their wording from here.
"""
from typing import Dict, Iterable, List, Optional, Sequence

#: The thresholds in storage order within a characteristic.
THRESHOLDS = ("RO", "OR", "RC", "COC")

#: The transition each threshold triggers when output speed crosses it.
THRESHOLD_TEXT = {
    "RO": "Regulated → Open",
    "OR": "Open → Regulated",
    "RC": "Regulated → Closed",
    "COC": "Closed → Overclosed",
}

#: The longer explanation, for tooltips and descriptions.
THRESHOLD_NOTE = {
    "RO": "below this output speed the clutch releases from regulated slip to "
          "open (the converter works hydraulically)",
    "OR": "above this output speed the clutch leaves open and begins regulated "
          "slip (or closes directly when slip regulation is unavailable)",
    "RC": "above this output speed regulated slip ends and the clutch is fully "
          "closed (locked); it drops back to regulated below RC minus the "
          "closed-to-regulated hysteresis",
    "COC": "above this output speed the closed clutch goes to overclosed (full "
           "apply pressure); it drops back below COC minus the "
           "overclosed-to-closed hysteresis. 10000 = disabled",
}

#: ZF's value for "threshold disabled".
DISABLED = 10000

#: Characteristics per WK map.
PER_MAP = 10

_ORD = {1: "1st", 2: "2nd", 3: "3rd"}


def ordinal(g: int) -> str:
    return _ORD.get(g, "%dth" % g)


def thresholds_for(cols: int) -> Sequence[str]:
    """The thresholds a map of ``cols`` columns stores per characteristic."""
    per = cols // PER_MAP if cols and cols % PER_MAP == 0 else 0
    return THRESHOLDS[:per] if 3 <= per <= 4 else ()


def characteristic_of(map_index: int, col: int, cols: int) -> Optional[int]:
    """1-based characteristic number of column ``col`` in WK map ``map_index``
    (1-based: WT_NWK_SK1 holds 1..10)."""
    per = len(thresholds_for(cols))
    if not per:
        return None
    return (map_index - 1) * PER_MAP + col // per + 1


def gears_text(gears: Optional[Iterable[int]]) -> str:
    """``gear 3`` / ``gears 3,4`` / ``unused``; "" when not known."""
    if gears is None:
        return ""
    g = sorted(set(gears))
    if not g:
        return "unused"
    return ("gear %d" % g[0]) if len(g) == 1 else "gears " + ",".join(map(str, g))


def column_header(k: int, thr: str, gears: Optional[Iterable[int]]) -> str:
    """Two-line column header: ``WK16 · gear 6`` / ``Regulated → Closed``.

    ``gears`` None means the image's gear-assignment table was not located, so
    no gear is claimed at all.
    """
    g = gears_text(gears)
    top = "WK%d · %s" % (k, g) if g else "WK%d" % k
    return "%s\n%s" % (top, THRESHOLD_TEXT[thr])


def column_tip(k: int, thr: str, gears: Optional[Iterable[int]],
               users: Optional[Dict[int, List[int]]] = None,
               ratio: Optional[float] = None, line_word: str = "SKL") -> str:
    """The tooltip: threshold, its meaning, gear(s), the programs using it."""
    parts = ["WK%d, threshold %s — %s: %s." % (
        k, thr, THRESHOLD_TEXT[thr], THRESHOLD_NOTE[thr])]
    if gears is None:
        parts.append("The gear-assignment table (WT_KWK_ZTAB) was not located "
                     "in this image, so the gear this characteristic serves "
                     "is not known.")
    elif not list(gears):
        parts.append("No line of WT_KWK_ZTAB selects this characteristic: "
                     "it is not used by any program.")
    else:
        for g in sorted(set(gears)):
            who = (users or {}).get(g) or []
            if who:
                parts.append("Destination gear %d, selected by %s %s." % (
                    g, line_word, ", ".join(str(w) for w in who)))
            else:
                parts.append("Destination gear %d." % g)
    parts.append("Output-shaft rpm%s." % (
        "; engine rpm = × %.3f (%s gear)" % (ratio, ordinal(sorted(set(gears))[0]))
        if ratio and gears and len(set(gears)) == 1 else ""))
    return " ".join(parts)


def usage(rows: Sequence[Sequence[int]]) -> Dict[int, Dict[int, List[int]]]:
    """``{characteristic: {gear: [line, ...]}}`` from WT_KWK_ZTAB lines
    (one list of per-gear characteristic numbers per line; 0 = none)."""
    out: Dict[int, Dict[int, List[int]]] = {}
    for line, row in enumerate(rows):
        for gi, c in enumerate(row):
            if c:
                out.setdefault(int(c), {}).setdefault(gi + 1, []).append(line)
    return out


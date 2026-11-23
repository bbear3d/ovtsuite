# SPDX-License-Identifier: MPL-2.0
# Copyright (c) 2026 Blair Campbell
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""Measuring-block layout for the ZF 6HP transmission controller (GS19).

Unlike the EDC16, whose block layout is a *table in the calibration*
(``UACC_RSigBlkGrp_MAP`` -> ESB -> signal catalogue, read by
:class:`~ovtsuite.datalog.channels.ChannelNamer`), the GS19's groups are fixed
by the controller software: there is nothing in the calibration to read them
from, and no A2L for this software number in the corpus. So the layout has to
be stated, and the honest source for it is the controller itself.

**Provenance.** Groups 1-17 below were read off a live 09L GS19 (the 6HP19 in
Bear's BHW-swapped A6 C6) on 2026-10-02, one screenshot per three groups, with
the car in P and the engine off -- which is why so many fields read zero. Group
18 answered "not available", so the set ends at 17. Field *order* and *units*
are what the controller returned; the names here are OVT-Suite's own, written
from the VAG component designations the controller reports (G93, G182, G195,
N88, N215-N233, N371, N110, F125, F189), not lifted from anyone's label file.

Three slots of group 15 came back unlabelled. Slot 2 is the selector lever --
confirmed against the car after the first reading, and it carries the same
value groups 4 and 13 report. Slots 1 and 3 stay unknown rather than guessed;
they still log, they are simply not role-resolvable.

Signal keys are prefixed ``Tcu_`` so they cannot collide with the engine's real
A2L measurement names in :mod:`ovtsuite.logtune.roles` -- these are OVT-Suite
identifiers for controller-fixed slots, not symbols out of a definition, and
the prefix keeps that distinction visible wherever a log column is resolved.
"""
import re
from typing import Dict, List, Optional, Tuple

#: A field is (label, signal key, unit). An empty key marks a slot whose
#: meaning is not established; it still logs, it just is not role-resolvable.
Field = Tuple[str, str, str]

#: group number -> its four slots, in the order the controller returns them.
GROUPS: Dict[int, Tuple[Field, Field, Field, Field]] = {
    1: (("Engine speed", "Tcu_nEng", "rpm"),
        ("Turbine speed (G182)", "Tcu_nTurb", "rpm"),
        ("Output shaft speed (G195)", "Tcu_nOut", "rpm"),
        ("Engaged gear", "Tcu_stGear", "")),
    2: (("Current shift program", "Tcu_stProg", ""),
        ("Accelerator pedal", "Tcu_rPed", "%"),
        ("Output shaft speed (G195)", "Tcu_nOut", "rpm"),
        ("Engaged gear", "Tcu_stGear", "")),
    3: (("Brake light switch", "Tcu_stBrake", ""),
        ("Shift lock solenoid (N110)", "Tcu_stShiftLock", ""),
        ("Vehicle speed", "Tcu_vVeh", "km/h"),
        ("Supply voltage (valves)", "Tcu_uValves", "V")),
    4: (("Fluid temperature (G93)", "Tcu_tAtf", "deg C"),
        ("Selector lever position", "Tcu_stLever", ""),
        ("Multi-function switch (F125)", "Tcu_stF125", ""),
        ("OBD info", "Tcu_stObd", "")),
    5: (("Solenoid valve 1 (N88)", "Tcu_stN88", ""),
        ("Pressure control valve 1 (N215)", "Tcu_iPcv1", "A"),
        ("Pressure control valve 2 (N216)", "Tcu_iPcv2", "A"),
        ("Engaged gear", "Tcu_stGear", "")),
    6: (("Pressure control valve 3 (N217)", "Tcu_iPcv3", "A"),
        ("Pressure control valve 4 (N218)", "Tcu_iPcv4", "A"),
        ("Pressure control valve 5 (N233)", "Tcu_iPcv5", "A"),
        ("Pressure control valve 6 (N371)", "Tcu_iPcv6", "A")),
    7: (("Fluid temperature (G93)", "Tcu_tAtf", "deg C"),
        ("Pressure control valve 6 (N371)", "Tcu_iPcv6", "A"),
        ("Converter clutch status", "Tcu_stTcc", ""),
        ("Converter slip", "Tcu_nTccSlip", "rpm")),
    8: (("Kick-down switch", "Tcu_stKickdown", ""),
        ("Accelerator pedal", "Tcu_rPed", "%"),
        ("Driving mode", "Tcu_stMode", ""),
        ("Control module temperature", "Tcu_tModule", "deg C")),
    9: (("Engine torque", "Tcu_trqEng", "Nm"),
        ("Engine speed", "Tcu_nEng", "rpm"),
        ("Accelerator pedal", "Tcu_rPed", "%"),
        ("Dynamic shift program", "Tcu_stDsp", "")),
    10: (("Converter amplification", "Tcu_facTcAmp", ""),
         ("Engine speed", "Tcu_nEng", "rpm"),
         ("Engaged gear", "Tcu_stGear", ""),
         ("Acceleration", "Tcu_aVeh", "m/s2")),
    11: (("Selector lever position", "Tcu_stLever", ""),
         ("Tiptronic switch (F189) recognition", "Tcu_stTipRec", ""),
         ("Tiptronic switch (F189) up/down", "Tcu_stTipDir", ""),
         ("Tiptronic buttons and paddles", "Tcu_stTipBtn", "")),
    12: (("Nominal engine torque", "Tcu_trqEngNom", "Nm"),
         ("Engine variant (ECU signal)", "Tcu_stEngVar", ""),
         ("Transmission variant (ECU signal)", "Tcu_stTrnVar", ""),
         ("CAN software level (ECU signal)", "Tcu_stCanLvl", "")),
    13: (("Selector lever position", "Tcu_stLever", ""),
         ("Shift lock solenoid (N110)", "Tcu_stShiftLock", ""),
         ("Multi-function switch (F125)", "Tcu_stF125", ""),
         ("Voltage (terminal 30)", "Tcu_uTerm30", "V")),
    14: (("Fluid temperature (G93)", "Tcu_tAtf", "deg C"),
         ("Control module temperature", "Tcu_tModule", "deg C"),
         ("Thermal protection", "Tcu_stThermal", ""),
         ("Maximum engine torque", "Tcu_trqEngMax", "Nm")),
    15: (("Group 15 / cell 1 (unlabelled)", "", ""),
         # Identified 2026-10-02 from the car: it tracks the selector lever,
         # the same value group 4 and group 13 report.
         ("Selector lever position", "Tcu_stLever", ""),
         ("Group 15 / cell 3 (unlabelled)", "", ""),
         ("Fluid temperature (G93)", "Tcu_tAtf", "deg C")),
    16: (("Accelerator pedal", "Tcu_rPed", "%"),
         ("Vehicle speed", "Tcu_vVeh", "km/h"),
         ("Current shift program", "Tcu_stProg", ""),
         ("Dynamic shift program", "Tcu_stDsp", "")),
    17: (("Control module afterrun", "Tcu_stAfterrun", ""),
         ("Supply voltage (terminal 30)", "Tcu_uTerm30", "V"),
         ("Supply voltage (terminal 15)", "Tcu_uTerm15", "V"),
         ("CAN information (terminal 15)", "Tcu_stCanT15", "")),
}

#: Group 18 answered "not available" on the controller read, so 17 is the end.
LAST_GROUP = 17


#: The gearbox codes of the 6HP19A, the box the layout above was read from.
#: The TCU says which gearbox it is in the text of its ``1A 9B`` identification
#: ("AG6 09L 3,0TFSI RdW"), the same code the part number of the gearbox
#: carries (09L927156C), which is a better key than the TCU's own part number:
#: 4F9910156A and 4E0910156A look alike, but 4E0910156A is a 09E (6HP26A).
#: Same table as ``ovtsuite.zf6hp.identification6hp.GEARBOX_BY_PART`` (09L and
#: 0AT are 6HP19A; 09E is the 6HP26A, whose structure differs and whose groups
#: nobody has read, so it is left out); kept here because the logger works
#: without the ZF module.
GEARBOX_CODES_6HP19 = ("09L", "0AT")
_DESIGNATION = re.compile(r"\bAG\d\s+([0-9A-Z]{3})\b")


def gearbox_code(part: str = "", identity: str = "") -> str:
    """The gearbox code a served identification names: the ``AG6 xxx``
    system designation in the identity text, else the part number's own
    prefix (a gearbox-level number such as 09L927156C). "" when neither."""
    m = _DESIGNATION.search((identity or "").upper())
    if m:
        return m.group(1)
    p = (part or "").upper().replace(" ", "")
    return p[:3] if re.match(r"[0-9A-Z]{3}927\d{3}", p) else ""


def is_6hp19(part: str = "", protocol: str = "", identity: str = "") -> bool:
    """Whether a served module is a ZF 6HP19A whose measuring blocks are the
    fixed layout above: KWP2000 (never UDS or KW1281: a 6HP28 over UDS has
    its own DIDs, :class:`Blocks6hpUds`) and a gearbox code of 09L or 0AT.
    A part number alone (4F9910156A) does not say which 6HP it is."""
    if (protocol or "").lower() != "kwp2000":
        return False
    return gearbox_code(part, identity) in GEARBOX_CODES_6HP19


def namer_for_part(part: str = "", protocol: str = "", identity: str = "") -> Optional["Blocks6hp"]:
    """The GS19 namer for a recognised 6HP19A, else None."""
    return Blocks6hp() if is_6hp19(part, protocol, identity) else None


class Blocks6hp:
    """Namer for the GS19's fixed blocks.

    Deliberately the same surface as
    :class:`~ovtsuite.datalog.channels.ChannelNamer`, so ``plan_groups`` and
    ``recipe_groups`` resolve a transmission preset with no idea which kind of
    controller they are planning for.
    """

    session = None

    def __init__(self, session=None):
        self.session = session

    @property
    def available(self) -> bool:
        return True

    def slots(self, group: int) -> int:
        return len(GROUPS.get(group, ())) or 4

    def resolve(self, group: int, field: int) -> Tuple[str, str, str]:
        g = GROUPS.get(group)
        generic = ("Group %d / Cell %d" % (group, field + 1), "", "")
        if g is None or field >= len(g):
            return generic
        label, signal, unit = g[field]
        if not signal:
            return (label, "", unit)
        return ("%s (%s)" % (label, signal), signal, unit)

    def channel(self, group: int, field: int):
        from ..datalog.logfile import Channel
        label, signal, unit = self.resolve(group, field)
        return Channel(group, field, label, unit, signal)

    def populated_groups(self) -> List[int]:
        return sorted(GROUPS)

    def group_summary(self, group: int) -> str:
        g = GROUPS.get(group)
        if g is None:
            return ""
        return ", ".join(f[0] for f in g if not f[0].startswith("Group "))

    def find_signal(self, name: str) -> Optional[Tuple[int, int]]:
        for grp in sorted(GROUPS):
            for f, fld in enumerate(GROUPS[grp]):
                if fld[1] == name:
                    return grp, f
        return None
